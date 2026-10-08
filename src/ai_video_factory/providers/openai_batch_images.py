"""Official OpenAI GPT Image generation for B2.

Each B2 image uses one standard image-generation request. Request state is
persisted before the POST so a lost response is never retried automatically.
The filename is retained for compatibility with existing local imports.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from itertools import chain
from pathlib import Path
from typing import Any

from PIL import Image

from .image_grids import GridPlanError, plan_image_jobs
from .openai_images import OpenAIImageClient, OpenAIImageRequestRejected
from .professional_image_state import finished_in_place_sha

_AVATAR_WORD = re.compile(r"\bavatar\b", flags=re.IGNORECASE)
_MAX_AVATAR_PNG = 14 * 1024 * 1024
_MAX_IMAGE_URL = 20_971_520
_MAX_PNG = 40 * 1024 * 1024
_BEAT_ID = re.compile(r"[1-9][0-9]*[A-Z]+", flags=re.ASCII)
_IMAGE_PREFIX = "iphone 6 photo done by an elderly:  "
_PROFESSIONAL_IMAGE_PREFIX = (
    "Render the following scene as authentic high-end iPhone 16/17 Pro Max photography, "
    "not DSLR or cinematic photography.  instagram rhode style. "
    "Preserve the described scene, composition, "
    "relationships, lighting, and mood, realistic skin texture, natural facial imperfections, "
    "authentic everyday environment, documentary feel, not polished, not cinematic, "
    "not luxury editorial."
)
_NUMBERED_PROFESSIONAL_PROMPT = re.compile(r"^([1-9]\.\s+)(.+)$", flags=re.DOTALL)
_GRID_IMAGE_QUALITY = "high"
# Tolerate spaces, punctuation after the marker, case and line breaks.
_FOR_CONTEXT = re.compile(r"\(\s*for\s+context\b", flags=re.IGNORECASE)
_AVATAR_REFERENCE_PROMPT_SUFFIX = (
    "set the reference image ONLY to preserve the avatar's general identity and appearance. "
    "Do NOT copy the reference image's lighting, camera quality, composition, background, "
    "color grading, framing, or professional photographic style.\n"
    "The photos should look genuinely amateur and casually taken by an elderly person with an "
    "iPhone 6"
)


class ImageGenerationError(RuntimeError):
    """The image is incomplete or needs safe, explicit recovery."""


# Compatibility for callers that imported the old provider exception. There is
# no Batch API path anymore.
ImageBatchError = ImageGenerationError


@dataclass(frozen=True)
class ImageOptions:
    model_id: str = "gpt-image-2.5-flare"
    size: str = "1280x720"
    quality: str = "low"
    max_parallel_images: int | None = None

    def __post_init__(self) -> None:
        if self.model_id not in {
            "gpt-image-2.5-flare",
            "gpt-image-2.5-sunburst",
        }:
            raise ValueError("Unsupported official GPT Image model")
        if self.size != "1280x720" or self.quality != "low":
            raise ValueError("B2 images must use 1280x720 and low quality")
        if self.max_parallel_images is not None and self.max_parallel_images < 1:
            raise ValueError("max_parallel_images must be positive when specified")

    def request(
        self,
        description: str,
        *,
        avatar_image_url: str | None = None,
        size: str | None = None,
        quality: str | None = None,
        visual_style: str = "casero",
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model_id,
            "prompt": effective_image_prompt(
                description, avatar_reference=avatar_image_url is not None,
                visual_style=visual_style,
            ),
            "size": size or self.size,
            "quality": self.quality if quality is None else quality,
            "output_format": "png",
            "n": 1,
        }
        if avatar_image_url is not None:
            request["images"] = [{"image_url": avatar_image_url}]
        return request


def _job_quality(job: dict[str, Any], options: ImageOptions) -> str:
    """Use high for grids, medium for context-marked standalone images."""
    return (
        _GRID_IMAGE_QUALITY if job.get("grid") is not None
        else "medium" if _FOR_CONTEXT.search(job["description"]) is not None
        else options.quality
    )


def effective_image_prompt(
    description: str, *, avatar_reference: bool = False,
    visual_style: str = "casero",
) -> str:
    """Apply application-owned image styling after B2 has described the scene."""
    if visual_style == "profesional":
        scene = description.strip()
        numbered = _NUMBERED_PROFESSIONAL_PROMPT.match(scene)
        if numbered is not None:
            return (
                numbered.group(1)
                + _PROFESSIONAL_IMAGE_PREFIX
                + " "
                + numbered.group(2).strip()
            )
        return _PROFESSIONAL_IMAGE_PREFIX + " " + scene
    if visual_style != "casero":
        raise ValueError("Unknown B2 visual style")
    prompt = (
        description
        if description.startswith("iPhone 6 photograph done by an elderly:\n\n")
        else _IMAGE_PREFIX + description
    )
    if avatar_reference:
        prompt += (
            "\n\nUse the attached avatar image as the character reference. "
            "Preserve the avatar's recognizable face and appearance while "
            "following the scene and composition in the description.\n\n"
            + _AVATAR_REFERENCE_PROMPT_SUFFIX
        )
    return prompt


def _avatar_requested(description: str) -> bool:
    return _AVATAR_WORD.search(description) is not None


def _avatar_reference(
    output: Path, plan: dict[str, Any], jobs: list[dict[str, Any]]
) -> tuple[str, str] | None:
    """Validate the selected avatar before any paid image request."""
    if not any(
        job.get("reference") if plan.get("visual_style") == "profesional"
        else _avatar_requested(job["description"])
        for job in jobs
    ):
        return None
    metadata = plan.get("avatar")
    if (
        not isinstance(metadata, dict)
        or metadata.get("file") != "avatar.png"
        or not isinstance(metadata.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
    ):
        raise ImageGenerationError("Avatar description requires the saved run avatar metadata")
    path = output / "avatar.png"
    if path.is_symlink() or not path.is_file():
        raise ImageGenerationError("Avatar description requires the saved run avatar.png")
    data = path.read_bytes()
    if not data or len(data) > _MAX_AVATAR_PNG:
        raise ImageGenerationError("Saved avatar PNG is empty or exceeds the reference size limit")
    digest = hashlib.sha256(data).hexdigest()
    if digest != metadata["sha256"]:
        raise ImageGenerationError("Saved avatar bytes differ from the selected run avatar")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG":
                raise ImageGenerationError("Saved run avatar is not PNG")
            image.verify()
    except (OSError, ValueError) as exc:
        raise ImageGenerationError("Saved run avatar contains invalid PNG bytes") from exc
    url = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    if len(url) > _MAX_IMAGE_URL:
        raise ImageGenerationError("Saved avatar exceeds the supported image reference size")
    return digest, url


def described_beats(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand each B2 beat into one or more independently addressable image assets."""
    blocks = plan.get("blocks")
    if not isinstance(blocks, list):
        raise ImageGenerationError("B2 visual plan is missing its blocks")
    result: list[dict[str, Any]] = []
    visual_style = plan.get("visual_style", "casero")
    if visual_style not in {"casero", "profesional"}:
        raise ImageGenerationError("Unknown B2 visual style")
    seen: set[tuple[int, str]] = set()

    def append_asset(
        *,
        block_id: int,
        beat: dict[str, Any],
        beat_id: str,
        description: str,
        asset_id: str,
        subshot_index: int = 1,
        subshot_count: int = 1,
        generation: str | None = None,
    ) -> None:
        if (
            not isinstance(description, str)
            or not description.strip()
            or not isinstance(asset_id, str)
            or not asset_id
        ):
            raise ImageGenerationError("Invalid described B2 image asset")
        key = block_id, asset_id
        if key in seen:
            raise ImageGenerationError(f"Duplicate described B2 asset: {asset_id}")
        seen.add(key)
        result.append({
            "block_id": block_id,
            "beat_id": beat_id,
            "asset_id": asset_id,
            "subshot_index": subshot_index,
            "subshot_count": subshot_count,
            "visual_type": beat.get("visual_type"),
            "text": beat.get("text"),
            "description": description,
            **({"generation": generation} if generation is not None else {}),
            **(
                {
                    "reference": beat["reference"],
                    "visual_style": visual_style,
                }
                if visual_style == "profesional" else {}
            ),
        })

    for block in blocks:
        block_id = block.get("block_id")
        if not isinstance(block_id, int) or isinstance(block_id, bool) or block_id < 1:
            raise ImageGenerationError("Invalid B2 block ID")
        beats = block.get("beats")
        if not isinstance(beats, list):
            raise ImageGenerationError("Invalid B2 beats")
        for beat in beats:
            beat_id = beat.get("beat_id")
            if (
                not isinstance(beat_id, str)
                or not _BEAT_ID.fullmatch(beat_id)
                or not beat_id.startswith(str(block_id))
            ):
                raise ImageGenerationError("Invalid B2 beat identifier")
            if visual_style == "profesional":
                if (
                    not isinstance(beat.get("reference"), bool)
                    or not isinstance(beat.get("audio"), bool)
                ):
                    raise ImageGenerationError(
                        "Professional B2 requires reference/audio booleans"
                    )
                subshots = beat.get("visual_subshots")
                if subshots is not None:
                    if beat.get("visual_type") != "media_image" or (
                        not isinstance(subshots, list) or len(subshots) != 2
                    ):
                        raise ImageGenerationError(
                            f"Invalid B2.1 visual_subshots for {beat_id}"
                        )
                    for expected_index, item in enumerate(subshots, start=1):
                        if (
                            not isinstance(item, dict)
                            or item.get("subshot_index") != expected_index
                            or item.get("subshot_count") != 2
                            or item.get("asset_id") != f"{beat_id}__{expected_index}"
                            or item.get("generation") not in {"grid", "independent"}
                        ):
                            raise ImageGenerationError(
                                f"Invalid B2.1 subshot metadata for {beat_id}"
                            )
                        append_asset(
                            block_id=block_id,
                            beat=beat,
                            beat_id=beat_id,
                            description=item.get("prompt"),
                            asset_id=item["asset_id"],
                            subshot_index=expected_index,
                            subshot_count=2,
                            generation=item["generation"],
                        )
                    continue

                description = beat.get("prompt")
                if description is None:
                    if beat.get("visual_type") != "avatar" or beat["reference"]:
                        raise ImageGenerationError("Only a reused avatar can omit its prompt")
                    continue
                if beat.get("visual_type") == "avatar" and not beat["reference"]:
                    raise ImageGenerationError("Regenerated avatar requires reference=true")
            else:
                description = beat.get("description")
                if description is None or (
                    isinstance(description, str) and not description.strip()
                ):
                    continue

            append_asset(
                block_id=block_id,
                beat=beat,
                beat_id=beat_id,
                description=description,
                asset_id=beat_id,
            )
    return result

def _fingerprint(job: dict[str, Any], options: ImageOptions) -> str:
    identity = {
        "beat_id": job["beat_id"],
        "asset_id": job.get("asset_id", job["beat_id"]),
        "block_id": job["block_id"],
        **options.request(
            job["description"], size=job.get("size"),
            quality=_job_quality(job, options),
            visual_style=job.get("visual_style", "casero"),
        ),
    }
    if job.get("avatar_reference_sha256") is not None:
        identity["prompt"] = effective_image_prompt(
            job["description"], avatar_reference=True,
            visual_style=job.get("visual_style", "casero"),
        )
        identity["image_endpoint"] = "/v1/images/edits"
        identity["avatar_reference_sha256"] = job["avatar_reference_sha256"]
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise ImageGenerationError(f"Refusing linked image state: {path}")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _job_paths(
    output: Path, job: dict[str, Any]
) -> tuple[Path, Path, Path]:
    asset_id = job.get("asset_id", job["beat_id"])
    if job.get("grid") is not None:
        folder = output / "images" / "grids"
        stem = f"block_{job['block_id']}_{asset_id}"
    else:
        folder = output / "images" / f"block_{job['block_id']}"
        stem = asset_id
    return folder, folder / f"{stem}.json", folder / f"{stem}.png"


def _mark_complete(
    job: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    image_path: Path,
    output: Path,
    generated: dict[str, Any],
    *,
    options: ImageOptions,
) -> dict[str, Any]:
    artifact = {
        **job,
        "provider": "openai_direct",
        "model_id": options.model_id,
        "size": generated["size"],
        "quality": _job_quality(job, options),
        "file": image_path.relative_to(output).as_posix(),
        "sha256": generated["sha256"],
        "width": generated["width"],
        "height": generated["height"],
        "openai_usage": generated.get("usage"),
        "openai_created": generated.get("created"),
        "status": "completed",
    }
    state["artifact"] = artifact
    state["sha256"] = generated["sha256"]
    state["status"] = "completed"
    state.pop("http_status", None)
    state.pop("error", None)
    _atomic_json(state_path, state)
    return artifact


def _one_image(
    job: dict[str, Any],
    output: Path,
    options: ImageOptions,
    *,
    api_key: str | None,
    direct_client: OpenAIImageClient | None,
    avatar_image_url: str | None,
) -> dict[str, Any]:
    reference_url = (
        avatar_image_url if job.get("avatar_reference_sha256") is not None else None
    )
    folder, state_path, image_path = _job_paths(output, job)
    if any(path.is_symlink() for path in (
        output / "images", folder, state_path, image_path
    )):
        raise ImageGenerationError(f"Refusing linked image assets for {job['beat_id']}")
    fingerprint = _fingerprint(job, options)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("schema_version") != "openai-image-v1":
            raise ImageGenerationError(
                f"Existing non-standard image state for {job['beat_id']} must be reconciled"
            )
        if state.get("fingerprint") != fingerprint:
            raise ImageGenerationError(
                f"B2 prompt changed for {job['beat_id']}; use a separate output root."
            )
    else:
        state = {
            **job,
            "schema_version": "openai-image-v1",
            "fingerprint": fingerprint,
            "model_id": options.model_id,
            "quality": _job_quality(job, options),
            "status": "direct_pending",
        }
        _atomic_json(state_path, state)

    if state["status"] == "completed":
        if image_path.is_file():
            actual_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
            if actual_sha == state.get("sha256"):
                return state["artifact"]
            if job.get("visual_style") == "profesional" and job.get("grid") is None:
                recipe = (
                    "image_x2" if job["visual_type"] in {"media_image", "avatar_media"}
                    else "native_video_crop"
                )
                accepted = finished_in_place_sha(
                    output, file=image_path.relative_to(output).as_posix(),
                    source_sha256=state["sha256"], block=job["block_id"],
                    beat=job.get("asset_id", job["beat_id"]), recipes=(recipe,),
                )
                if accepted == actual_sha:
                    return state["artifact"]
        raise ImageGenerationError(
            f"Completed PNG for {job['beat_id']} is missing or changed; "
            "do not automatically repeat a paid request."
        )
    if state["status"] == "direct_submitting_unknown":
        raise ImageGenerationError(
            f"Standard image POST for {job['beat_id']} has an unknown outcome. "
            f"Inspect {state_path}; never resubmit automatically."
        )
    if state["status"] == "direct_rejected":
        # A prior 4xx is a definite failed request. A later explicit run may
        # retry it while all completed paid images remain cached.
        state["status"] = "direct_pending"
        state.pop("http_status", None)
        state.pop("error", None)
        _atomic_json(state_path, state)
    if state["status"] != "direct_pending":
        raise ImageGenerationError(f"Unrecognized image state for {job['beat_id']}")
    if direct_client is None:
        if not api_key:
            raise ImageGenerationError(
                f"OPENAI_API_KEY is missing for image {job['beat_id']}"
            )
        direct_client = OpenAIImageClient(api_key)

    state["direct_started_at_unix"] = time.time()
    state["status"] = "direct_submitting_unknown"
    _atomic_json(state_path, state)
    try:
        kwargs: dict[str, Any] = {
            "prompt": effective_image_prompt(
                job["description"], avatar_reference=reference_url is not None,
                visual_style=job.get("visual_style", "casero"),
            ),
            "model": options.model_id,
            "size": job.get("size", options.size),
            "quality": _job_quality(job, options),
            "output_format": "png",
            "destination": image_path,
        }
        if reference_url is not None:
            kwargs["reference_image_url"] = reference_url
        generated = direct_client.generate_png(**kwargs)
    except OpenAIImageRequestRejected as exc:
        state["status"] = "direct_rejected"
        state["http_status"] = exc.status_code
        state["error"] = exc.api_message
        _atomic_json(state_path, state)
        raise ImageGenerationError(
            f"Standard image POST for {job['beat_id']} was rejected by OpenAI "
            f"with HTTP {exc.status_code}: {exc.api_message}. "
            "Completed image beats remain cached; rerun images-only after correcting "
            "the rejection cause."
        ) from exc
    except Exception as exc:
        raise ImageGenerationError(
            f"Standard image POST outcome for {job['beat_id']} is unknown. "
            f"Inspect {state_path}; never resubmit automatically."
        ) from exc
    return _mark_complete(
        job, state, state_path, image_path, output, generated, options=options
    )


def generate_b2_images(
    output: Path,
    plan: dict[str, Any],
    *,
    api_key: str | None,
    options: ImageOptions,
    direct_client: OpenAIImageClient | None = None,
) -> dict[str, Any]:
    """Generate independent standard OpenAI image requests safely."""
    described = described_beats(plan)
    try:
        jobs = plan_image_jobs(plan, described)
    except GridPlanError as exc:
        raise ImageGenerationError(str(exc)) from exc
    reference = _avatar_reference(output, plan, described)
    visual_style = plan.get("visual_style", "casero")
    if reference is not None:
        digest, _url = reference
        jobs = [
            {**job, "avatar_reference_sha256": digest}
            if any(
                beat.get("reference") if visual_style == "profesional"
                else _avatar_requested(beat["description"])
                for beat in job.get("panels", [job])
            )
            else job
            for job in jobs
        ]

    planned_grid_states: set[Path] = set()
    for job in jobs:
        folder, state_path, _image = _job_paths(output, job)
        if job.get("grid") is not None:
            planned_grid_states.add(state_path)
            for panel in job["panels"]:
                old_state = (
                    output / "images" / f"block_{panel['block_id']}"
                    / f"{panel.get('asset_id', panel['beat_id'])}.json"
                )
                if old_state.exists() or old_state.is_symlink():
                    raise ImageGenerationError(
                        f"Existing individual paid image state at {old_state}; "
                        "reconcile it before grouping this beat in a grid."
                    )
        if folder.is_symlink() or state_path.is_symlink():
            raise ImageGenerationError("Refusing linked image state")
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("schema_version") != "openai-image-v1":
                raise ImageGenerationError(
                    f"Existing non-standard image state at {state_path}; "
                    "reconcile it before using standard image generation."
                )
            if state.get("fingerprint") != _fingerprint(job, options):
                raise ImageGenerationError(
                    f"Image request differs from saved beat {job['beat_id']}; "
                    "use another output root."
                )
    grid_root = output / "images" / "grids"
    if grid_root.is_symlink():
        raise ImageGenerationError("Refusing linked grid directory")
    for old_state in grid_root.glob("*.json"):
        if old_state not in planned_grid_states:
            raise ImageGenerationError(
                f"Saved grid state {old_state} is not in the current plan; "
                "reconcile it before generating new paid images."
            )
    if jobs and direct_client is None and not api_key:
        raise ImageGenerationError("OPENAI_API_KEY is required for official GPT images")

    manifest_path = output / "images" / "manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "openai-b2-images-v1",
        "provider": "openai",
        "model_id": options.model_id,
        "size": options.size,
        "quality": options.quality,
        "grid_quality": _GRID_IMAGE_QUALITY,
        "max_parallel_images": min(options.max_parallel_images or len(jobs), len(jobs)),
        "status": "running",
        "total_described_beats": len({
            (item["block_id"], item["beat_id"]) for item in described
        }),
        "total_described_assets": len(described),
        "total_generation_jobs": len(jobs),
        "grid_count": sum(job.get("grid") is not None for job in jobs),
        "openai_direct_count": 0,
        "items": [],
    }
    _atomic_json(manifest_path, manifest)
    results: list[dict[str, Any] | None] = [None] * len(jobs)
    failures: list[tuple[str, Exception]] = []
    try:
        if jobs:
            worker_count = min(options.max_parallel_images or len(jobs), len(jobs))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {
                    pool.submit(
                        _one_image, job, output, options, api_key=api_key,
                        direct_client=direct_client,
                        avatar_image_url=reference[1] if reference is not None else None,
                    ): (i, job.get("asset_id", job["beat_id"]))
                    for i, job in enumerate(jobs)
                }
                for future in as_completed(futures):
                    index, beat_id = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception as exc:
                        failures.append((beat_id, exc))
                        print(f"  Image {beat_id}: {exc}", flush=True)
                    manifest["items"] = [item for item in results if item is not None]
                    manifest["openai_direct_count"] = sum(
                        item["provider"] == "openai_direct"
                        for item in manifest["items"]
                    )
                    _atomic_json(manifest_path, manifest)
        if failures:
            detail = "; ".join(f"{beat}: {exc}" for beat, exc in failures)
            raise ImageGenerationError(
                f"{len(failures)} of {len(jobs)} image beats failed; "
                f"completed beats were saved: {detail}"
            ) from failures[0][1]
        manifest["status"] = "completed"
        _atomic_json(manifest_path, manifest)
    except Exception:
        manifest["status"] = "incomplete"
        _atomic_json(manifest_path, manifest)
        raise
    return manifest


def clear_previous_b2_images(output: Path, plan: dict[str, Any]) -> int:
    """Archive replaceable image artifacts before an images-only run."""
    described = described_beats(plan)
    try:
        jobs = plan_image_jobs(plan, described)
    except GridPlanError as exc:
        raise ImageGenerationError(str(exc)) from exc
    image_root = output / "images"
    if image_root.is_symlink():
        raise ImageGenerationError("Refusing linked image directory")
    crop_records: dict[str, str] = {}
    manifest_path = image_root / "manifest.json"
    if manifest_path.is_symlink():
        raise ImageGenerationError("Refusing linked image manifest")
    if manifest_path.is_file():
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        panels = prior.get("panel_assets", [])
        if not isinstance(panels, list):
            raise ImageGenerationError("Invalid previous cropped panel manifest")
        for panel in panels:
            if (
                not isinstance(panel, dict)
                or not isinstance(panel.get("file"), str)
                or not isinstance(panel.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", panel["sha256"]) is None
            ):
                raise ImageGenerationError("Invalid saved cropped panel record")
            crop_records[panel["file"]] = panel["sha256"]

    assets: list[tuple[Path, Path, Path, Path]] = []
    for job in jobs:
        folder, state_path, image_path = _job_paths(output, job)
        legacy_batch_path = image_path.with_suffix(".batch.jsonl")
        if any(path.is_symlink() for path in (
            folder, state_path, image_path, legacy_batch_path
        )):
            raise ImageGenerationError(f"Refusing linked image assets for {job['beat_id']}")
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("schema_version") == "openai-image-v1":
                continue
            raise ImageGenerationError(
                f"Existing non-standard image state at {state_path}; "
                "reconcile it before using standard image generation."
            )
        crop_sha = crop_records.get(image_path.relative_to(output).as_posix())
        if crop_sha is not None and image_path.is_file():
            actual = hashlib.sha256(image_path.read_bytes()).hexdigest()
            if actual != crop_sha:
                finishing = None
                for beat in described:
                    if (
                        beat["block_id"] == job["block_id"]
                        and beat.get("asset_id", beat["beat_id"])
                        == job.get("asset_id", job["beat_id"])
                    ):
                        recipe = (
                            "image_x2"
                            if beat["visual_type"] in {"media_image", "avatar_media"}
                            else "native_grid_video"
                        )
                        finishing = finished_in_place_sha(
                            output, file=image_path.relative_to(output).as_posix(),
                            source_sha256=crop_sha, block=beat["block_id"],
                            beat=beat.get("asset_id", beat["beat_id"]),
                            recipes=(recipe,),
                        )
                        break
                if finishing != actual:
                    raise ImageGenerationError(
                        f"Saved grid crop changed at {image_path}; reconcile manually"
                    )
            continue
        if state_path.is_file() or image_path.is_file() or legacy_batch_path.is_file():
            assets.append((state_path, image_path, legacy_batch_path, folder))

    archive = output / "images" / "replaced_states"
    if archive.is_symlink():
        raise ImageGenerationError("Refusing linked replaced-state archive")
    stamp = uuid.uuid4().hex
    removed = 0
    for state_path, image_path, batch_path, folder in assets:
        if state_path.is_file():
            destination = archive / folder.name / f"{state_path.stem}.{stamp}.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            state_path.replace(destination)
        if batch_path.is_file():
            destination = archive / folder.name / f"{batch_path.stem}.{stamp}.jsonl"
            destination.parent.mkdir(parents=True, exist_ok=True)
            batch_path.replace(destination)
        if image_path.is_file():
            image_path.unlink()
            removed += 1
    return removed


def clear_b2_images_for_rerun(output: Path) -> int:
    """Delete the complete B2 image tree before an explicit B2 rerun.

    A B2 rerun produces a new visual plan, so image states from the previous
    plan cannot be used for fingerprint validation. Unfinished/ambiguous paid
    requests are left in place and block the rerun rather than being silently
    replaced.
    """
    image_root = output / "images"
    if not image_root.exists():
        return 0
    if image_root.is_symlink() or image_root.is_junction():
        raise ImageGenerationError("Refusing linked image directory")

    # New runs keep finished PNGs in their original beat paths and store only
    # verification metadata under finishing_state. Accept legacy outputs too.
    finished_roots = (
        image_root / "finishing_state",
        image_root / "professional_finished",
    )
    finished_archive = output / "images_replaced_professional_finishing"
    if (finished_archive.is_symlink() or finished_archive.is_junction()
        or any(root.is_symlink() for root in finished_roots)):
        raise ImageGenerationError("Refusing linked professional finishing state")
    for finished_root in finished_roots:
        for finished_state in finished_root.glob("block_*/*.json"):
            if finished_state.is_symlink() or finished_state.parent.is_symlink():
                raise ImageGenerationError("Linked professional finishing state")
            saved = json.loads(finished_state.read_text(encoding="utf-8"))
            if (saved.get("schema_version") != "professional-image-finishing-v1"
                or saved.get("status") != "completed"):
                raise ImageGenerationError(
                    f"Professional image finishing is unresolved at {finished_state}; "
                    "reconcile it before rerunning B2"
                )
            if finished_root.name == "finishing_state":
                relative = saved.get("output_file")
                if (not isinstance(relative, str) or not relative.startswith("images/")
                    or ".." in Path(relative).parts):
                    raise ImageGenerationError("Invalid in-place finished image destination")
                image_path = output / relative
                if not image_path.is_relative_to(image_root):
                    raise ImageGenerationError("Finished image escapes image directory")
            else:
                image_path = finished_state.with_suffix(".png")
            if (
                not image_path.is_file() or image_path.is_symlink()
                or hashlib.sha256(image_path.read_bytes()).hexdigest() != saved.get("sha256")
            ):
                raise ImageGenerationError(
                    f"Professional image finishing is unresolved at {finished_state}; "
                    "reconcile it before rerunning B2"
                )
    for state_path in chain(
        image_root.glob("block_*/*.json"),
        (image_root / "grids").glob("*.json"),
        (image_root / "b3_shots").glob("*.json"),
    ):
        if state_path.is_symlink():
            raise ImageGenerationError(f"Refusing linked image state: {state_path}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("status") != "completed":
            raise ImageGenerationError(
                f"Previous paid image task is unfinished at {state_path}; "
                "reconcile it before rerunning B2."
            )
        image_path = state_path.with_suffix(".png")
        if not image_path.is_file():
            raise ImageGenerationError(
                f"Completed image is missing for {state_path}; reconcile it before rerunning B2."
            )

    removed = sum(1 for _ in image_root.rglob("*.png"))
    # Keep all original paid GPT request metadata for billing, not only the
    # historical finishing metadata. A prior grid may now be the only paid
    # request in a professional run.
    historical_archive = output / "images_replaced_b2"
    if historical_archive.is_symlink() or historical_archive.is_junction():
        raise ImageGenerationError("Refusing linked B2 billing archive")
    paid_states = [
        *image_root.glob("block_*/*.json"),
        *(image_root / "grids").glob("*.json"),
        *(image_root / "b3_shots").glob("*.json"),
        *((image_root / "replaced_states").glob("block_*/*.json")),
    ]
    if paid_states:
        destination = historical_archive / ("run-" + uuid.uuid4().hex)
        for state_path in paid_states:
            if state_path.is_symlink() or state_path.parent.is_symlink():
                raise ImageGenerationError("Refusing linked historical paid image state")
            saved_path = destination / state_path.relative_to(image_root)
            saved_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.replace(saved_path)
    for finished_root in finished_roots:
        if finished_root.is_dir():
            # Preserve paid native-edit JSON evidence for future billing audits.
            finished_archive.mkdir(parents=True, exist_ok=True)
            destination = finished_archive / ("run-" + uuid.uuid4().hex)
            finished_root.replace(destination)
    for child in image_root.iterdir():
        if child.is_symlink():
            raise ImageGenerationError(f"Refusing linked image asset: {child}")
    for child in image_root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    return removed


def has_pending_image_tasks(output: Path) -> bool:
    """Protect durable image requests before a B1/B2 output replacement."""
    roots = (
        output / "images" / "finishing_state",
        output / "images" / "professional_finished",
    )
    if (output / "images").is_symlink() or any(root.is_symlink() for root in roots):
        return True
    for root in roots:
        for path in root.glob("block_*/*.json"):
            if path.is_symlink() or path.parent.is_symlink():
                return True
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if root.name == "finishing_state":
                    relative = state.get("output_file")
                    if (not isinstance(relative, str) or not relative.startswith("images/")
                        or ".." in Path(relative).parts):
                        return True
                    image = output / relative
                    if not image.is_relative_to(output / "images"):
                        return True
                else:
                    image = path.with_suffix(".png")
                if (state.get("schema_version") != "professional-image-finishing-v1"
                    or state.get("status") != "completed"
                    or image.is_symlink() or not image.is_file()
                    or hashlib.sha256(image.read_bytes()).hexdigest() != state.get("sha256")):
                    return True
            except (OSError, ValueError, TypeError):
                return True
    for state_path in chain(
        (output / "images").glob("block_*/*.json"),
        (output / "images" / "grids").glob("*.json"),
        (output / "images" / "b3_shots").glob("*.json"),
    ):
        if state_path.is_symlink():
            return True
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("status") != "completed":
            return True
        if not state_path.with_suffix(".png").is_file():
            return True
        if state.get("artifact", {}).get("batch_may_complete_later"):
            return True
    return False
