"""B2 -> LTX orchestration: one I2V per video beat, one A2V per avatar run.

Postgres owns durable job identity; R2 owns cached outputs. Never reissue a
different paid request when an old request's result is ambiguous.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import wave
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ai_video_factory.compositor.static_preview import (
    _verified_asset,
    build_stt_beat_timeline,
)
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.gpu_failures import (
    DEFAULT_GPU_MAX_ATTEMPTS,
    classify_gpu_failure,
)
from ai_video_factory.inference.ports import ObjectStorage
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.providers.inference_jobs import (
    InferenceJobExecutor,
    cached_inference_response,
)
from ai_video_factory.providers.job_queue import (
    JobQueueClient,
    QueueJobNotFoundError,
    QueueJobStatus,
)
from ai_video_factory.workers.ltx25.a2v import (
    LTX_A2V_RECOMMENDED_MAX_SECONDS,
    LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE,
    LTX_A2V_RUN_OPTIMIZED_PROFILE,
    LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE,
    LTX_A2V_RUN_VAE_BACKEND,
    LTX_A2V_TASK,
)
from ai_video_factory.workers.ltx25.jobs import (
    ltx_a2v_application_job_id,
    ltx_video_application_job_id,
)
from ai_video_factory.workers.ltx25.model import (
    LTX_PRODUCTION_I2V_PROFILE,
    LTX_VIDEO_TASK,
    ltx_num_frames_for_duration,
)
from ai_video_factory.workers.ltx25.reference_recipe import (
    LTX_A2V_STATIC_NEGATIVE_PROMPT,
    reference_num_frames_for_samples,
)

AVATAR_PROMPT = "A Person speaking to the camera."
AVATAR_NEGATIVE_PROMPT = (
    "camera zoom, zoom in, push in, dolly in, camera drift, camera shake, "
    "reframing, crop change, handheld camera, unintended camera movement"
)
if AVATAR_NEGATIVE_PROMPT != LTX_A2V_STATIC_NEGATIVE_PROMPT:
    raise RuntimeError("Pinned LTX static-reference negative prompt changed")

_FPS = 24
_WIDTH = 1920
_HEIGHT = 1080
_I2V_MAX_ATTEMPTS = 2
_RATE = 48000
_A2V_MAX_RAW_FRAMES = 1024
_SCHEMA = "b2-ltx-video-v2-1080p"


class B2VideoError(RuntimeError):
    """A B2 video cannot be generated or joined without breaking its contracts."""


@dataclass(frozen=True, slots=True)
class B2VideoJob:
    kind: Literal["i2v", "a2v"]
    beats: tuple[str, ...]
    first_index: int
    last_index: int
    start_seconds: float
    end_seconds: float
    image: Path
    audio: Path | None
    request: InferenceJobRequest

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


def _save_json(path: Path, data: dict[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise B2VideoError(f"Refusing linked video manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    if part.is_symlink():
        raise B2VideoError(f"Refusing linked temporary manifest: {part}")
    try:
        part.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def _image_asset_maps(
    output: Path, plan: dict[str, Any], images: dict[str, Any],
) -> tuple[dict[tuple[int, str], Path], dict[tuple[int, str], Path]]:
    """Return parent-beat primary assets plus every immutable image asset."""
    assets: dict[tuple[int, str], Path] = {}
    all_assets: dict[tuple[int, str], Path] = {}
    for item in (
        images["final_assets"] if plan.get("visual_style") == "profesional"
        else [
            *(entry for entry in images.get("items", []) if entry.get("grid") is None),
            *images.get("panel_assets", []),
        ]
    ):
        block_id, beat_id = item["block_id"], item["beat_id"]
        asset_id = item.get("asset_id", beat_id)
        identity = block_id, asset_id
        if identity in all_assets:
            raise B2VideoError(f"Duplicate B2 image asset for {identity}")
        path = _verified_asset(output, item.get("file"), item.get("sha256"))
        all_assets[identity] = path
        if item.get("subshot_index") not in (None, 1):
            continue
        key = block_id, beat_id
        if key in assets:
            raise B2VideoError(f"Duplicate primary B2 image for {key}")
        assets[key] = path
    return assets, all_assets


def _source_assets(
    output: Path, plan: dict[str, Any], images: dict[str, Any],
) -> tuple[Path, dict[tuple[int, str], Path]]:
    """Return the historical LTX contract: avatar plus one primary image per beat."""
    if images.get("status") != "completed":
        raise B2VideoError("LTX requires a completed B2 image manifest")
    avatar_meta = plan.get("avatar")
    if not isinstance(avatar_meta, dict):
        raise B2VideoError("LTX requires the saved run avatar")
    avatar = _verified_asset(output, avatar_meta.get("file"), avatar_meta.get("sha256"))
    if plan.get("visual_style") == "profesional" and (
        images.get("finishing_status") != "completed"
    ):
        raise B2VideoError(
            "Professional LTX requires finished still images "
            "and verified video sources"
        )
    assets, _all_assets = _image_asset_maps(output, plan, images)
    return avatar, assets


def _has_visual_subshots(plan: dict[str, Any]) -> bool:
    return any(
        isinstance(beat.get("visual_subshots"), list)
        for block in plan.get("blocks", [])
        for beat in block.get("beats", [])
    )

def _b3_shot_assets(
    output: Path, plan: dict[str, Any],
) -> dict[tuple[int, str], Path]:
    assets: dict[tuple[int, str], Path] = {}
    for item in plan.get("b3_shot_assets", []):
        if not isinstance(item, dict):
            raise B2VideoError("Invalid B3 shot asset record")
        block_id, beat_id = item.get("block_id"), item.get("beat_id")
        if not isinstance(block_id, int) or not isinstance(beat_id, str):
            raise B2VideoError("Invalid B3 shot asset identity")
        key = block_id, beat_id
        if key in assets:
            raise B2VideoError(f"Duplicate B3 shot image for {beat_id}")
        assets[key] = _verified_asset(output, item.get("file"), item.get("sha256"))
    return assets


def _group_beats(beats: list[dict[str, Any]]) -> list[tuple[str, int, int]]:
    """Group A2V by B3 take boundaries when present, else by historical source."""
    groups: list[tuple[str, int, int]] = []
    avatar_start: int | None = None
    avatar_source: tuple[object, ...] | None = None
    previous_cta: int | None = None
    for index, beat in enumerate(beats):
        # CTA speaking takes must not cross their group boundaries even
        # when B3 skips short (1-2 beat) groups.
        current_cta = beat.get("cta_group_id")
        if index and current_cta != previous_cta and avatar_start is not None:
            groups.append(("a2v", avatar_start, index - 1))
            avatar_start, avatar_source = None, None
        previous_cta = current_cta
        kind = beat["visual_type"]
        if kind == "media_image" and beat.get("audio") is True:
            raise B2VideoError(
                f"Speaking media_image beat {beat['beat_id']} is invalid; "
                "use media_video for synchronized speech"
            )
        speaks = kind in {"avatar", "avatar_media"} or beat.get("audio") is True
        if speaks:
            if "continuous" in beat:
                continuous = beat.get("continuous")
                if not isinstance(continuous, bool):
                    raise B2VideoError(
                        f"B3 continuous flag is invalid for {beat['beat_id']}"
                    )
                if not continuous:
                    if avatar_start is not None:
                        groups.append(("a2v", avatar_start, index - 1))
                    avatar_start = index
                    avatar_source = ("b3_take", beat["beat_id"])
                elif avatar_start is None:
                    raise B2VideoError(
                        f"B3 beat {beat['beat_id']} continues without a take start"
                    )
                continue
            if kind in {"media_image", "media_video"} or (
                kind == "avatar" and beat.get("prompt") is not None
            ):
                source = (beat["block_id"], beat["beat_id"])
            elif kind in {"avatar", "avatar_media"}:
                source = ("original_avatar",)
            else:
                raise B2VideoError(f"Unsupported speaking visual type: {kind!r}")
            if avatar_start is not None and source != avatar_source:
                groups.append(("a2v", avatar_start, index - 1))
                avatar_start = None
            if avatar_start is None:
                avatar_start, avatar_source = index, source
            continue
        if avatar_start is not None:
            groups.append(("a2v", avatar_start, index - 1))
            avatar_start, avatar_source = None, None
        if kind == "media_video":
            groups.append(("i2v", index, index))
        elif kind != "media_image":
            raise B2VideoError(f"Unsupported B2 visual type: {kind!r}")
    if avatar_start is not None:
        groups.append(("a2v", avatar_start, len(beats) - 1))
    return groups


def _extract_pcm(soundtrack: Path, destination: Path, start: float, end: float) -> str:
    """One WAV per contiguous avatar run, sliced from the actual narration master.

    This includes any 500-ms inter-block silence inside the avatar group.
    No per-beat audio is submitted, decoded or independently concatenated.
    """
    first = round(start * _RATE)
    last = round(end * _RATE)
    if first < 0 or last <= first:
        raise B2VideoError("Invalid avatar audio group bounds")
    if destination.is_symlink() or destination.parent.is_symlink():
        raise B2VideoError("Refusing linked group audio path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(".part.wav")
    if part.is_symlink():
        raise B2VideoError("Refusing linked temporary group audio path")
    part.unlink(missing_ok=True)
    try:
        with wave.open(str(soundtrack), "rb") as original:
            if (
                original.getframerate() != _RATE or original.getnchannels() != 1
                or original.getsampwidth() != 2 or last > original.getnframes()
            ):
                raise B2VideoError("Avatar audio group exceeds the validated PCM narration")
            original.setpos(first)
            pcm = original.readframes(last - first)
        if len(pcm) != 2 * (last - first):
            raise B2VideoError("Incomplete PCM audio group")
        with wave.open(str(part), "wb") as joined:
            joined.setnchannels(1)
            joined.setsampwidth(2)
            joined.setframerate(_RATE)
            joined.writeframes(pcm)
        part.replace(destination)
    finally:
        part.unlink(missing_ok=True)
    return sha256_file(destination)


def _fingerprint(
    timeline: dict[str, Any], audio: dict[str, Any],
    assets: dict[tuple[int, str], Path], avatar: Path,
    *, profile: str | None = None,
    i2v_profile: str | None = LTX_PRODUCTION_I2V_PROFILE,
    b3_assets: dict[tuple[int, str], Path] | None = None,
    all_assets: dict[tuple[int, str], Path] | None = None,
    motion_overrides: dict[str, str] | None = None,
    cta_signature: dict[str, Any] | None = None,
) -> str:
    relevant = {
        "schema": _SCHEMA,
        "timeline": timeline["beats"],
        "master_audio_sha256": audio["sha256"],
        "avatar_sha256": sha256_file(avatar),
        "image_sha256": [
            [block_id, asset_id, sha256_file(path)]
            for (block_id, asset_id), path in sorted(
                (all_assets or assets).items()
            )
        ],
        "b3_image_sha256": [
            [block_id, beat_id, sha256_file(path)]
            for (block_id, beat_id), path in sorted((b3_assets or {}).items())
        ],
        "profile": profile or LTX_A2V_RUN_OPTIMIZED_PROFILE,
        "positive_prompt": AVATAR_PROMPT,
        "negative_prompt": AVATAR_NEGATIVE_PROMPT,
        "fps": _FPS,
        "width": _WIDTH,
        "height": _HEIGHT,
    }
    # Only I2V-containing B2 runs need a new directory when its recipe changes;
    # historical v4 fingerprints omitted this key entirely.
    if i2v_profile is not None and any(
        beat["visual_type"] == "media_video" and not beat.get("audio")
        for beat in timeline["beats"]
    ):
        relevant["i2v_profile"] = i2v_profile
    if motion_overrides:
        relevant["b22_motion_overrides"] = sorted(motion_overrides.items())
    if cta_signature:
        relevant["professional_ebook_cta"] = cta_signature
    canonical = json.dumps(relevant, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_b2_video_plan(
    output: Path, plan: dict[str, Any], audio: dict[str, Any],
    images: dict[str, Any], *,
    motion_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[B2VideoJob], Path]:
    """Validate all sources and durations BEFORE queuing any paid inference."""
    timeline = build_stt_beat_timeline(output, plan, audio)
    beats = timeline["beats"]
    previous_frame = 0
    for beat in beats:
        first = round(beat["start_seconds"] * _FPS)
        last = round(beat["end_seconds"] * _FPS)
        if first != previous_frame or last <= first:
            raise B2VideoError(
                f"B2 beat {beat['beat_id']} cannot be cut exactly at {_FPS} fps; "
                "no paid LTX requests were submitted"
            )
        previous_frame = last
    if previous_frame != round(timeline["duration_seconds"] * _FPS):
        raise B2VideoError("B2 frame timeline differs from master audio duration")
    avatar, assets = _source_assets(output, plan, images)
    all_assets = (
        _image_asset_maps(output, plan, images)[1]
        if _has_visual_subshots(plan)
        else assets
    )
    b3_assets = _b3_shot_assets(output, plan)
    for beat in beats:
        key = beat["block_id"], beat["beat_id"]
        if (
            beat["visual_type"] != "avatar" or beat.get("prompt") is not None
        ) and key not in assets:
            raise B2VideoError(f"Missing generated B2 PNG for {beat['beat_id']}")
        if beat["visual_type"] == "media_video":
            if not isinstance(beat["description"], str) or not beat["description"].strip():
                raise B2VideoError(f"Missing media_video description for {beat['beat_id']}")
            if "video_prompt" in beat and (
                not isinstance(beat["video_prompt"], str)
                or not beat["video_prompt"].strip()
            ):
                raise B2VideoError(
                    f"Missing media_video video_prompt for {beat['beat_id']}"
                )
    digest = _fingerprint(
        timeline, audio, assets, avatar,
        b3_assets=b3_assets, all_assets=all_assets,
        motion_overrides=motion_overrides,
        cta_signature={
            "cta_groups": plan.get("ebook_cta_groups", []),
            "qr_sha256": (
                plan.get("avatar", {}).get("qr", {}).get("sha256")
                if plan.get("ebook_cta_groups") else None
            ),
        } if plan.get("ebook_cta_groups") else None,
    )
    run_dir = output / "video" / "runs" / digest[:20]
    if run_dir.is_symlink() or run_dir.parent.is_symlink():
        raise B2VideoError("Refusing linked B2 LTX run directory")
    groups = _group_beats(beats)
    master = _verified_asset(output, audio.get("file"), audio.get("sha256"))

    # The pinned reference cannot represent an arbitrarily long audio run.
    # Never silently split consecutive avatars into separately generated clips.
    for kind, first, last in groups:
        if kind == "a2v":
            count = round(beats[last]["end_seconds"] * _RATE) - round(
                beats[first]["start_seconds"] * _RATE
            )
            if count > round(LTX_A2V_RECOMMENDED_MAX_SECONDS * _RATE):
                raise B2VideoError(
                    "Consecutive avatar beats exceed the supported single A2V "
                    f"speech duration ({beats[first]['beat_id']}–"
                    f"{beats[last]['beat_id']}, {count / _RATE:.2f}s > "
                    f"{LTX_A2V_RECOMMENDED_MAX_SECONDS:.0f}s). "
                    "Split the avatar timeline before any paid inference."
                )
            try:
                reference_num_frames_for_samples(
                    count, sample_rate=_RATE, fps=_FPS,
                    max_raw_frames=_A2V_MAX_RAW_FRAMES,
                )
            except ValueError as exc:
                raise B2VideoError(
                    "Consecutive avatar beats exceed the supported single A2V "
                    f"generation ({beats[first]['beat_id']}–{beats[last]['beat_id']}); "
                    "no paid jobs were submitted"
                ) from exc

    jobs: list[B2VideoJob] = []
    for ordinal, (kind, first, last) in enumerate(groups, start=1):
        selection = beats[first:last + 1]
        beat_ids = tuple(beat["beat_id"] for beat in selection)
        start, end = selection[0]["start_seconds"], selection[-1]["end_seconds"]
        seed = 4242 + ordinal
        if kind == "i2v":
            beat = selection[0]
            image = assets[(beat["block_id"], beat["beat_id"])]
            image_sha = sha256_file(image)
            num_frames = ltx_num_frames_for_duration(end - start, fps=_FPS)
            prompt = beat["description"]
            if isinstance(beat.get("video_prompt"), str):
                # New professional B2: the separately authored motion prompt
                # controls LTX I2V instead of reusing the still-image text.
                prompt = beat["video_prompt"]
            elif beat.get("prompt") is not None:
                # Historical professional plans predate video_prompt; do not
                # silently reinterpret their cached paid requests.
                prompt = (
                    "Animate the supplied start image in one continuous realistic take. "
                    "Show the single beat-supported action or natural process described "
                    f"by the narration: {beat['text']}. "
                    "Preserve the image identity, setting and framing; no cuts or invented events."
                )
            if motion_overrides and beat_ids[0] in motion_overrides:
                prompt = motion_overrides[beat_ids[0]]
            job_id = f"b2-{beat_ids[0]}-" + ltx_video_application_job_id(
                shot_id=ordinal, prompt=prompt, keyframe_sha256=image_sha,
                seed=seed, width=_WIDTH, height=_HEIGHT, fps=_FPS,
                num_frames=num_frames, generation_profile=LTX_PRODUCTION_I2V_PROFILE,
            )
            key = f"b2-ltx/inputs/images/{image_sha}.png"
            request = InferenceJobRequest(
                job_id=job_id, task=LTX_VIDEO_TASK,
                # Defense in depth for deployed workers: expensive 1080p I2V
                # never receives the generic five-attempt budget.
                max_attempts=_I2V_MAX_ATTEMPTS,
                inputs=[ObjectInput(
                    name="keyframe", key=key, sha256=image_sha,
                    content_type="image/png",
                )],
                output=ObjectOutput(
                    key=f"jobs/{job_id}/b2_{beat_ids[0]}.mp4",
                    content_type="video/mp4",
                ),
                parameters={
                    "generation_profile": LTX_PRODUCTION_I2V_PROFILE,
                    "prompt": prompt, "seed": seed,
                    "width": _WIDTH, "height": _HEIGHT, "fps": _FPS,
                    "num_frames": num_frames,
                },
            )
            audio_path = None
        else:
            first_beat = selection[0]
            key = first_beat["block_id"], first_beat["beat_id"]
            if first_beat.get("b3_shot_prompt") is not None:
                if key not in b3_assets:
                    raise B2VideoError(
                        f"Missing B3 start image for {first_beat['beat_id']}"
                    )
                image = b3_assets[key]
            elif first_beat["visual_type"] in {"media_image", "media_video"} or (
                first_beat["visual_type"] == "avatar"
                and first_beat.get("prompt") is not None
            ):
                image = assets[key]
            else:
                image = avatar
            image_sha = sha256_file(image)
            a2v_prompt = AVATAR_PROMPT
            if isinstance(first_beat.get("b3_shot_video_prompt"), str):
                a2v_prompt += " " + first_beat["b3_shot_video_prompt"]
            elif (
                first_beat["visual_type"] == "media_video"
                and isinstance(first_beat.get("video_prompt"), str)
            ):
                # Historical B2-only plans still use their speaking motion prompt.
                a2v_prompt += " " + first_beat["video_prompt"]
            if motion_overrides and first_beat["beat_id"] in motion_overrides:
                a2v_prompt = (
                    AVATAR_PROMPT + " " + motion_overrides[first_beat["beat_id"]]
                )
            audio_path = run_dir / "audio" / f"{beat_ids[0]}-{beat_ids[-1]}.wav"
            audio_sha = _extract_pcm(master, audio_path, start, end)
            job_id = ltx_a2v_application_job_id(
                segment_id=f"b2-{beat_ids[0]}-{beat_ids[-1]}",
                prompt=a2v_prompt, image_sha256=image_sha,
                audio_sha256=audio_sha, seed=seed, width=_WIDTH, height=_HEIGHT,
                fps=_FPS, generation_profile=LTX_A2V_RUN_OPTIMIZED_PROFILE,
            )
            request = InferenceJobRequest(
                job_id=job_id, task=LTX_A2V_TASK, max_attempts=DEFAULT_GPU_MAX_ATTEMPTS,
                inputs=[
                    ObjectInput(
                        name="image", key=f"b2-ltx/inputs/avatars/{image_sha}.png",
                        sha256=image_sha, content_type="image/png",
                    ),
                    ObjectInput(
                        name="audio", key=f"b2-ltx/inputs/audio/{audio_sha}.wav",
                        sha256=audio_sha, content_type="audio/wav",
                    ),
                ],
                output=ObjectOutput(
                    key=f"jobs/{job_id}/b2_avatar.mp4",
                    content_type="video/mp4",
                ),
                sidecar_outputs={"metadata": ObjectOutput(
                    key=f"jobs/{job_id}/metadata.json",
                    content_type="application/json",
                )},
                parameters={
                    "generation_profile": LTX_A2V_RUN_OPTIMIZED_PROFILE,
                    "prompt": a2v_prompt, "seed": seed,
                    "width": _WIDTH, "height": _HEIGHT, "fps": _FPS,
                    "vae_backend": LTX_A2V_RUN_VAE_BACKEND,
                    "guidance_forward_batch_size": (
                        LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE
                    ),
                    "progressive_block_group_size": (
                        LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE
                    ),
                },
            )
        jobs.append(B2VideoJob(
            kind=kind, beats=beat_ids, first_index=first, last_index=last,
            start_seconds=start, end_seconds=end, image=image,
            audio=audio_path, request=request,
        ))
    return timeline, jobs, run_dir


def _probe_video(path: Path, job: B2VideoJob) -> None:
    if shutil.which("ffprobe") is None:
        raise B2VideoError("ffprobe is required to validate LTX outputs")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height,avg_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode:
        raise B2VideoError(f"Cannot probe LTX output: {probe.stderr[-800:]}")
    data = json.loads(probe.stdout)
    video = [entry for entry in data["streams"] if entry["codec_type"] == "video"]
    audio = [entry for entry in data["streams"] if entry["codec_type"] == "audio"]
    if len(video) != 1 or (video[0]["width"], video[0]["height"]) != (_WIDTH, _HEIGHT):
        raise B2VideoError("LTX output video geometry/streams differ from contract")
    if job.kind == "a2v" and len(audio) != 1:
        raise B2VideoError("A2V worker must return its own speech audio stream")
    rate = video[0].get("avg_frame_rate", "0/1").split("/")
    if len(rate) != 2 or int(rate[1]) == 0 or abs(
        int(rate[0]) / int(rate[1]) - _FPS
    ) > 0.01:
        raise B2VideoError("LTX output has the wrong frame rate")
    duration = float(data["format"]["duration"])
    if not math.isfinite(duration) or duration < job.duration_seconds - 2 / _FPS:
        raise B2VideoError("LTX output is too short for the source beat timeline")


def _new_manifest(jobs: list[B2VideoJob], audio: dict[str, Any], digest: str) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA, "status": "running", "fingerprint": digest,
        "master_audio_sha256": audio["sha256"],
        "avatar_prompt": AVATAR_PROMPT,
        "avatar_negative_prompt": AVATAR_NEGATIVE_PROMPT,
        "jobs": [
            {
                "kind": job.kind, "beat_ids": list(job.beats),
                "job_id": job.request.job_id,
                "request_sha256": job.request.fingerprint(),
                "status": "pending",
            }
            for job in jobs
        ],
    }


def _legacy_run_metadata_fingerprint(
    request: InferenceJobRequest, *, run_id: str, run_job_count: int,
) -> str:
    """Fingerprint the broken contract that put scaling data in model parameters."""
    parameters = dict(request.parameters)
    parameters.update(run_id=run_id, run_job_count=run_job_count)
    return request.model_copy(update={"parameters": parameters}).fingerprint()


def _migrate_legacy_run_metadata_manifest(
    manifest: dict[str, Any], jobs: list[B2VideoJob], *, run_id: str,
) -> tuple[bool, bool]:
    """Rewrite only manifests proven to differ by misplaced orchestration metadata."""
    records = manifest.get("jobs")
    if not isinstance(records, list) or len(records) != len(jobs):
        return False, False
    counts = Counter(job.kind for job in jobs)
    migrated = False
    for job, record in zip(jobs, records, strict=True):
        if not isinstance(record, dict) or (
            record.get("kind") != job.kind
            or record.get("beat_ids") != list(job.beats)
            or record.get("job_id") != job.request.job_id
        ):
            return False, False
        current_sha = job.request.fingerprint()
        saved_sha = record.get("request_sha256")
        if saved_sha == current_sha:
            continue
        if record.get("status") == "completed" or saved_sha != (
            _legacy_run_metadata_fingerprint(
                job.request,
                run_id=run_id,
                run_job_count=counts[job.kind],
            )
        ):
            return False, False
        record["request_sha256"] = current_sha
        migrated = True
    if migrated:
        manifest["request_contract_migration"] = "ltx-run-metadata-postgres-v1"
    return True, migrated


def has_pending_b2_video(output: Path) -> bool:
    """Protect in-flight Postgres/R2 jobs from destructive B1.1 resets."""
    root = output / "video" / "runs"
    if root.is_symlink():
        return True
    if not root.exists():
        return False
    for path in root.glob("*/manifest.json"):
        if path.is_symlink():
            return True
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("status") not in {"completed", "superseded"}:
            return True
    return False


def reconcile_obsolete_b2_video_runs(
    output: Path,
    plan: dict[str, Any],
    audio: dict[str, Any],
    images: dict[str, Any],
    *,
    queue: JobQueueClient,
) -> dict[str, object]:
    """Cancel unresolved jobs from an obsolete B2 video recipe/source fingerprint.

    This explicit maintenance operation is used when images or the production
    LTX recipe change after a prior run already published paid work.
    Succeeded/failed/cancelled jobs are preserved as historical paid artifacts.
    Jobs reusable by the new immutable plan are untouched. Pending/retryable
    obsolete work is cancelled. Actively leased work is never force-cancelled.
    """
    _timeline, jobs, run_dir = build_b2_video_plan(output, plan, audio, images)
    new_digest = run_dir.name
    reusable_job_ids = {job.request.job_id for job in jobs}
    runs = output / "video" / "runs"
    if runs.is_symlink():
        raise B2VideoError("Refusing linked LTX video runs directory")
    if not runs.exists():
        return {"new_digest": new_digest, "superseded_runs": 0, "cancelled_jobs": 0}

    superseded = 0
    cancelled = 0
    for previous_path in sorted(runs.glob("*/manifest.json")):
        if previous_path.is_symlink() or previous_path.parent.is_symlink():
            raise B2VideoError("Refusing linked previous LTX manifest")
        old = json.loads(previous_path.read_text(encoding="utf-8"))
        if not isinstance(old, dict) or not isinstance(old.get("jobs"), list):
            raise B2VideoError(f"Invalid previous LTX manifest: {previous_path}")
        if old.get("fingerprint") == new_digest or old.get("status") in {
            "completed", "superseded",
        }:
            continue
        if old.get("master_audio_sha256") != audio.get("sha256"):
            raise B2VideoError(
                "A previous incomplete LTX run has a different soundtrack; "
                "refusing B2 video reconciliation"
            )

        for record in old["jobs"]:
            if not isinstance(record, dict):
                raise B2VideoError("Invalid previous LTX job record")
            job_id = record.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise B2VideoError("Previous LTX job has no durable ID")
            if job_id in reusable_job_ids:
                continue
            try:
                snapshot = queue.get(job_id)
            except QueueJobNotFoundError:
                if record.get("status") == "pending":
                    continue
                raise B2VideoError(
                    f"Previous LTX job {job_id} is missing from Postgres"
                ) from None

            if snapshot.status in {
                QueueJobStatus.SUCCEEDED,
                QueueJobStatus.FAILED,
                QueueJobStatus.CANCELLED,
            }:
                continue
            try:
                queue.cancel(job_id)
            except RuntimeError as exc:
                raise B2VideoError(
                    f"Previous LTX job {job_id} is still actively leased; "
                    "refusing to force-cancel paid inference"
                ) from exc
            cancelled += 1

        previous_status = old.get("status")
        old["status"] = "superseded"
        old["superseded_by"] = new_digest
        old["superseded_reason"] = "explicit B2 video reconciliation"
        old["previous_status"] = previous_status
        _save_json(previous_path, old)
        superseded += 1

    return {
        "new_digest": new_digest,
        "superseded_runs": superseded,
        "cancelled_jobs": cancelled,
    }


def _reconcile_prior_failed_video_runs(
    output: Path,
    *,
    new_digest: str,
    audio_sha256: str,
    queue: JobQueueClient,
) -> None:
    """Supersede only old OOM runs proven terminal in authoritative Postgres.

    Do not recreate paid jobs while an old request is pending, running,
    succeeded-but-not-materialized, cancelled or in an unknown state.
    """
    runs = output / "video" / "runs"
    if runs.is_symlink():
        raise B2VideoError("Refusing linked LTX video runs directory")
    for previous_path in sorted(runs.glob("*/manifest.json")):
        if previous_path.is_symlink() or previous_path.parent.is_symlink():
            raise B2VideoError("Refusing linked previous LTX manifest")
        old = json.loads(previous_path.read_text(encoding="utf-8"))
        if not isinstance(old, dict) or not isinstance(old.get("jobs"), list):
            raise B2VideoError(f"Invalid previous LTX manifest: {previous_path}")
        if old.get("fingerprint") == new_digest or old.get("status") in {
            "completed", "superseded"
        }:
            continue
        if old.get("master_audio_sha256") != audio_sha256:
            raise B2VideoError(
                "A previous incomplete LTX run has a different soundtrack; "
                "reconcile it before generating a new video"
            )
        for record in old["jobs"]:
            if not isinstance(record, dict):
                raise B2VideoError("Invalid previous LTX job record")
            if record.get("status") == "completed":
                continue
            old_id = record.get("job_id")
            if not isinstance(old_id, str) or not old_id:
                raise B2VideoError("Previous LTX job has no durable ID")
            try:
                snapshot = queue.get(old_id)
            except QueueJobNotFoundError:
                if record.get("status") == "pending":
                    continue  # Manifest saved before any authoritative submission.
                raise B2VideoError(
                    f"Previous LTX job {old_id} is missing from Postgres; "
                    "cannot verify whether its paid inference completed"
                ) from None
            if snapshot.status is not QueueJobStatus.FAILED:
                raise B2VideoError(
                    f"Previous LTX job {old_id} is {snapshot.status.value}; "
                    "refusing to submit another paid video while its result is unresolved"
                )
            provider = snapshot.provider_payload or {}
            last_error = provider.get("last_error")
            failure_kind = (
                classify_gpu_failure(RuntimeError(last_error))
                if isinstance(last_error, str) else None
            )
            if failure_kind not in {"oom", "decoder_budget"} or (
                failure_kind == "decoder_budget" and record.get("kind") != "a2v"
            ):
                raise B2VideoError(
                    f"Previous LTX job {old_id} failed for a non-OOM/non-DiffVAE-budget "
                    "or unknown reason; manual reconciliation is required before retrying"
                )
        prior_status = old["status"]
        old["status"] = "superseded"
        old["superseded_by"] = new_digest
        old["superseded_reason"] = "Postgres-confirmed terminal OOM or A2V DiffVAE budget"
        old["previous_status"] = prior_status
        _save_json(previous_path, old)
        print(
            f"  Previous LTX run {previous_path.parent.name} has terminal GPU memory failure; "
            "preserving its artifacts and using new memory-safe request IDs",
            flush=True,
        )


def generate_b2_ltx_video(
    output: Path, plan: dict[str, Any], audio: dict[str, Any],
    images: dict[str, Any], *, queue: JobQueueClient, storage: ObjectStorage,
    poll_seconds: float = 15.0, timeout_seconds: float = 21600.0,
    check_capacity: Callable[[], None] | None = None,
    check_a2v_worker: Callable[[], None] | None = None,
    check_i2v_worker: Callable[[], None] | None = None,
    casero_cta_dir: Path | None = None,
) -> dict[str, Any]:
    """Durably submit exactly one LTX request per video beat/avatar group, then mux.

    The caller owns workers and autoscaler availability; this client never
    allocates Salad resources or silently falls back to still images.
    """
    from ai_video_factory.compositor.b_video import render_b2_ltx_video
    from ai_video_factory.workflows.casero_cta_video import (
        cta_assets,
        current_cta_ready,
        finish_cta,
        insert_cta,
        submit_cta_first,
    )

    timeline, jobs, run_dir = build_b2_video_plan(output, plan, audio, images)
    if plan.get("visual_style") == "profesional":
        # B2.2 sees the real finalized LTX start PNGs *after* B3 grouping.
        # Produce and validate every override before any paid job submission.
        from ai_video_factory.workflows.b22_motion import adapt_b22_motion

        _avatar, context_images = _source_assets(output, plan, images)
        # The STT timeline has timing and motion fields, but deliberately
        # omits frozen narration text/beat_type. Restore these from the
        # authoritative visual plan for B2.2 whole-sequence reasoning.
        source_beats = {
            beat["beat_id"]: beat
            for block in plan["blocks"] for beat in block["beats"]
        }
        context_timeline = {
            **timeline,
            "beats": [
                {**source_beats[beat["beat_id"]], **beat}
                for beat in timeline["beats"]
            ],
        }
        motion_overrides = adapt_b22_motion(
            output, context_timeline, jobs, context_images,
        )
        if motion_overrides:
            timeline, jobs, run_dir = build_b2_video_plan(
                output, plan, audio, images, motion_overrides=motion_overrides,
            )
    digest = run_dir.name
    manifest_path = run_dir / "manifest.json"
    expected = _new_manifest(jobs, audio, digest)
    if manifest_path.is_symlink():
        raise B2VideoError("Refusing linked LTX job manifest")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        matches, migrated = _migrate_legacy_run_metadata_manifest(
            manifest, jobs, run_id=digest
        )
        if manifest.get("fingerprint") != digest or not matches:
            raise B2VideoError("Saved LTX manifest differs; reconcile paid jobs first")
        if migrated:
            _save_json(manifest_path, manifest)
            print(
                "  Reconciled legacy LTX run metadata contract; preserving job identity.",
                flush=True,
            )
    else:
        manifest = expected
        _save_json(manifest_path, manifest)
    if check_a2v_worker is not None and any(
        job.kind == "a2v" and record["status"] != "completed"
        for job, record in zip(jobs, manifest["jobs"], strict=True)
    ):
        # Fail before any paid I2V/A2V submissions if the new worker image
        # is not deployed. A repo pull never updates Salad's pinned Docker image.
        check_a2v_worker()
    _reconcile_prior_failed_video_runs(
        output, new_digest=digest, audio_sha256=audio["sha256"], queue=queue
    )
    executor = InferenceJobExecutor(
        queue=queue, storage=storage,
        poll_seconds=poll_seconds, timeout_seconds=timeout_seconds,
        pending_timeout_seconds=timeout_seconds,
    )

    pending_jobs: list[tuple[B2VideoJob, dict[str, Any], Path]] = []
    for job, record in zip(jobs, manifest["jobs"], strict=True):
        clip = run_dir / "clips" / f"{job.request.job_id}.mp4"
        if record["status"] == "completed":
            _verified_asset(output, record.get("file"), record.get("sha256"))
            _probe_video(clip, job)
            continue
        if clip.is_symlink() or clip.parent.is_symlink():
            raise B2VideoError("Refusing linked LTX output path")
        clip.parent.mkdir(parents=True, exist_ok=True)
        pending_jobs.append((job, record, clip))

    # The promoted profile is only executable after a compatible immutable
    # I2V image has been deployed. Do not submit its paid jobs to an old worker.
    if check_i2v_worker is not None and any(
        job.kind == "i2v" for job, _record, _clip in pending_jobs
    ):
        check_i2v_worker()

    # Preserve the existing fail-closed guard: no new input upload or paid job
    # publication happens while the singleton Capacity Controller is unhealthy.
    if casero_cta_dir is not None:
        cta_assets(casero_cta_dir)
    if (pending_jobs or casero_cta_dir is not None) and check_capacity is not None:
        check_capacity()
    if (
        casero_cta_dir is not None
        and check_a2v_worker is not None
        and not current_cta_ready(casero_cta_dir)
    ):
        check_a2v_worker()
    # Submit CTA first on the shared Postgres A2V queue, BEFORE normal B2 jobs.
    cta_submitted = (
        submit_cta_first(casero_cta_dir, executor, queue)
        if casero_cta_dir is not None else None
    )

    uncached_jobs: list[B2VideoJob] = []
    for job, _record, _clip in pending_jobs:
        if cached_inference_response(storage, job.request) is not None:
            continue
        uncached_jobs.append(job)
        print(
            f"  LTX {job.kind.upper()} beats={','.join(job.beats)}: "
            f"preparing Postgres job {job.request.job_id}",
            flush=True,
        )
        for item in job.request.inputs:
            source = job.audio if item.name == "audio" else job.image
            if source is None:
                raise B2VideoError("Missing LTX source input")
            executor.ensure_input(
                source, key=item.key, sha256=item.sha256,
                content_type=item.content_type,
                metadata={"purpose": f"b2-ltx-{job.kind}"},
            )

    # Publish the full uncached backlog before waiting for any single clip.
    # This lets the singleton Capacity Controller observe true LTX demand and
    # scale horizontal Salad capacity. Postgres submission is idempotent by
    # deterministic job_id/request fingerprint; cached R2 bundles are never
    # re-queued here, so resume/recovery cannot create duplicate paid inference.
    for job in uncached_jobs:
        snapshot = queue.submit(
            job.request,
            metadata={
                "capability": f"b2-ltx-{job.kind}",
                "run_id": digest,
            },
        )
        print(
            f"  LTX {job.kind.upper()} job={job.request.job_id}: "
            f"published to shared Postgres queue status={snapshot.status.value}",
            flush=True,
        )

    for job, record, clip in pending_jobs:
        # execute() deliberately remains the authoritative wait/recovery path.
        # The preceding submit is idempotent, so this second submit cannot
        # duplicate work and preserves all existing terminal-bundle recovery.
        print(
            f"  LTX {job.kind.upper()} job={job.request.job_id}: "
            "waiting for a Salad worker/result",
            flush=True,
        )
        response = executor.execute(
            job.request,
            metadata={
                "capability": f"b2-ltx-{job.kind}",
                "run_id": digest,
            },
        )
        print(
            f"  LTX {job.kind.upper()} job={job.request.job_id}: "
            "inference completed; verifying MP4",
            flush=True,
        )
        part = clip.with_suffix(".part.mp4")
        if part.is_symlink():
            raise B2VideoError("Refusing linked temporary LTX clip")
        part.unlink(missing_ok=True)
        try:
            executor.download_output(response, part)
            _probe_video(part, job)
            if job.kind == "a2v":
                sidecar = job.request.sidecar_outputs["metadata"]
                meta = run_dir / "clips" / f"{job.request.job_id}.metadata.json"
                stored = storage.stat(sidecar.key)
                if stored is None:
                    raise B2VideoError("Missing A2V generation metadata sidecar")
                storage.download(sidecar.key, meta)
                if sha256_file(meta) != stored.metadata.get("artifact-sha256"):
                    raise B2VideoError("A2V metadata SHA-256 mismatch")
                values = json.loads(meta.read_text(encoding="utf-8"))
                if (
                    values.get("generation_profile") !=
                    LTX_A2V_RUN_OPTIMIZED_PROFILE
                    or values.get("negative_prompt_applied") is not True
                    or values.get("positive_prompt_enhanced") is not False
                    or values.get("quantization") != "fp8_cast"
                    or values.get("offload_mode") != "none"
                    or values.get("diffvae_backend_effective") != "triton"
                    or values.get("vae_backend_request") != LTX_A2V_RUN_VAE_BACKEND
                    or values.get("guidance_forward_batch_size")
                    != LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE
                    or values.get("progressive_block_group_size")
                    != LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE
                    or values.get("stage1_residency") != "gpu"
                    or values.get("stage2_residency") != "progressive_cpu"
                    or values.get("stage_1_image_strength") != 0.8
                    or values.get("video_cfg_scale") != 1.25
                ):
                    raise B2VideoError("A2V did not apply the pinned static camera guidance")
            part.replace(clip)
        finally:
            part.unlink(missing_ok=True)
        record.update(
            status="completed",
            file=clip.relative_to(output).as_posix(),
            sha256=sha256_file(clip),
            duration_seconds=job.duration_seconds,
            replayed=response.replayed,
        )
        _save_json(manifest_path, manifest)

    if manifest.get("status") == "completed":
        _verified_asset(output, manifest.get("file"), manifest.get("sha256"))
        if casero_cta_dir is not None and "cta_insertion" not in manifest:
            raise B2VideoError(
                "Completed video predates CTA insertion; reconcile prior publication"
            )
        return manifest
    result = render_b2_ltx_video(output, run_dir, timeline, jobs, manifest, images, audio)
    if plan.get("visual_style") == "profesional" and plan.get("ebook_cta_groups"):
        from ai_video_factory.compositor.professional_cta_qr import (
            apply_professional_ebook_qr,
        )

        result = apply_professional_ebook_qr(
            output, run_dir, result, timeline, plan,
        )
    if casero_cta_dir is not None:
        finish_cta(casero_cta_dir, cta_submitted, executor)
        raw = json.loads((output / "B1.1" / "input.json").read_text(encoding="utf-8"))
        source_script = raw["plain_script_for_recording"]
        inserted = insert_cta(
            output, source_script, plan, audio,
            output / result["file"], casero_cta_dir, run_dir / "with_cta.mp4",
        )
        result.update(inserted)
        result["duration_seconds"] += inserted["cta_insertion"]["cta_duration_seconds"]
        result["audio_source"] = "verified_original_pcm_with_per_avatar_casero_cta"
    manifest.update(result)
    manifest["status"] = "completed"
    _save_json(manifest_path, manifest)
    _save_json(output / "video" / "latest.json", {
        "manifest_file": manifest_path.relative_to(output).as_posix(),
        "file": manifest["file"], "sha256": manifest["sha256"],
        "fingerprint": digest, "status": "completed",
    })
    return manifest
