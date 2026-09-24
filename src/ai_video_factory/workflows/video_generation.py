from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoClip, VideoPrompt
from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    InferenceJobResponse,
    ObjectInput,
    ObjectOutput,
)
from ai_video_factory.inference.ports import ObjectStorage
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.providers.images import inspect_image_payload
from ai_video_factory.providers.inference_jobs import cached_inference_response
from ai_video_factory.providers.job_queue import (
    JobQueueClient,
    QueueJobNotFoundError,
    QueueJobSnapshot,
    QueueJobStatus,
    TransientQueueError,
)
from ai_video_factory.workers.ltx25.jobs import ltx_video_application_job_id
from ai_video_factory.workers.ltx25.model import (
    LTX_GENERATION_PROFILE,
    LTX_VIDEO_TASK,
    ltx_num_frames_for_duration,
)
from ai_video_factory.workflows._video_jobs import (
    archive_manifest,
    download_verified_mp4,
    terminal_failure_detail,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VideoGenerationPlanItem:
    shot_id: int
    keyframe_path: Path
    keyframe_sha256: str
    input_key: str
    request: InferenceJobRequest


class VideoGenerationJobState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_id: int = Field(ge=1)
    application_job_id: str
    request_sha256: str
    transport_job_id: str | None = None
    transport_status: Literal[
        "unsubmitted", "pending", "running", "succeeded", "failed", "cancelled"
    ] = "unsubmitted"
    submission_count: int = Field(default=0, ge=0)
    response: InferenceJobResponse | None = None
    last_terminal_transport_job_id: str | None = None
    last_terminal_payload: dict[str, Any] | None = None


class VideoGenerationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    run_fingerprint: str
    transport_route: str | None = None
    jobs: list[VideoGenerationJobState] = Field(min_length=1)


class VideoGenerationIncompleteError(RuntimeError):
    """Raised when one or more transport jobs reached a terminal non-success state."""


def build_video_generation_plan(
    keyframes: list[StoryboardKeyframe],
    prompts: list[VideoPrompt],
    timings: list[ShotTiming],
    *,
    keyframe_base_dir: Path,
    width: int = 1280,
    height: int = 720,
    fps: int = 24,
    seed_base: int = 42,
) -> list[VideoGenerationPlanItem]:
    """Build deterministic LTX queue requests for every canonical shot."""

    _validate_inputs(keyframes, prompts, timings)
    if (width, height, fps) != (1280, 720, 24):
        raise ValueError("Production LTX output must be exactly 1280x720 at 24 fps")

    base_dir = keyframe_base_dir.resolve()
    plan: list[VideoGenerationPlanItem] = []

    for keyframe, prompt, timing in zip(keyframes, prompts, timings, strict=True):
        keyframe_path = (base_dir / keyframe.uri).resolve()
        if not keyframe_path.is_relative_to(base_dir):
            raise ValueError(f"Shot {keyframe.shot_id} keyframe URI escapes its base directory")
        if not keyframe_path.is_file():
            raise FileNotFoundError(f"Storyboard keyframe file not found: {keyframe_path}")

        image_format, keyframe_width, keyframe_height = inspect_image_payload(
            keyframe_path.read_bytes()
        )
        if image_format != "png":
            raise ValueError(f"Shot {keyframe.shot_id} keyframe must be a PNG")
        if keyframe_width * 9 != keyframe_height * 16:
            raise ValueError(
                f"Shot {keyframe.shot_id} keyframe must be native 16:9; "
                f"found {keyframe_width}x{keyframe_height}"
            )
        if keyframe_width < width or keyframe_height < height:
            raise ValueError(
                f"Shot {keyframe.shot_id} keyframe is below the 1280x720 LTX input contract: "
                f"found {keyframe_width}x{keyframe_height}"
            )

        duration_seconds = timing.end_seconds - timing.start_seconds
        num_frames = ltx_num_frames_for_duration(duration_seconds, fps=fps)
        seed = seed_base + keyframe.shot_id
        keyframe_sha256 = sha256_file(keyframe_path)
        job_id = ltx_video_application_job_id(
            shot_id=keyframe.shot_id,
            prompt=prompt.prompt,
            keyframe_sha256=keyframe_sha256,
            seed=seed,
            width=width,
            height=height,
            fps=fps,
            num_frames=num_frames,
        )
        input_key = f"phase8/keyframes/{keyframe_sha256}.png"
        output_key = f"jobs/{job_id}/shot_{keyframe.shot_id:03d}.mp4"
        request = InferenceJobRequest(
            job_id=job_id,
            task=LTX_VIDEO_TASK,
            inputs=[
                ObjectInput(
                    name="keyframe",
                    key=input_key,
                    sha256=keyframe_sha256,
                    content_type="image/png",
                )
            ],
            output=ObjectOutput(key=output_key, content_type="video/mp4"),
            parameters={
                "generation_profile": LTX_GENERATION_PROFILE,
                "prompt": prompt.prompt,
                "seed": seed,
                "width": width,
                "height": height,
                "fps": fps,
                "num_frames": num_frames,
            },
        )
        plan.append(
            VideoGenerationPlanItem(
                shot_id=keyframe.shot_id,
                keyframe_path=keyframe_path,
                keyframe_sha256=keyframe_sha256,
                input_key=input_key,
                request=request,
            )
        )

    return plan


def video_generation_run_fingerprint(plan: list[VideoGenerationPlanItem]) -> str:
    if not plan:
        raise ValueError("Video generation plan cannot be empty")
    payload = [
        {
            "shot_id": item.shot_id,
            "application_job_id": item.request.job_id,
            "request_sha256": item.request.fingerprint(),
        }
        for item in plan
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_video_generation(
    plan: list[VideoGenerationPlanItem],
    *,
    queue: JobQueueClient,
    storage: ObjectStorage,
    manifest_path: Path,
    clips_dir: Path,
    clip_uri_prefix: str = "video_clips",
    wait: bool = True,
    retry_terminal: bool = True,
    poll_seconds: float = 15.0,
    timeout_seconds: float = 21600.0,
    dispatch_timeout_seconds: float = 300.0,
    transport_route: str | None = None,
) -> tuple[VideoGenerationManifest, list[VideoClip]]:
    """Fan out LTX jobs, persist progress, resume transports and fan in canonical clips."""

    if poll_seconds <= 0 or timeout_seconds <= 0:
        raise ValueError("poll_seconds and timeout_seconds must be positive")
    if dispatch_timeout_seconds <= 0:
        raise ValueError("dispatch_timeout_seconds must be positive")
    if not plan:
        raise ValueError("Video generation plan cannot be empty")

    manifest = _load_or_create_manifest(
        plan,
        manifest_path,
        transport_route=transport_route,
    )
    state_by_shot = {state.shot_id: state for state in manifest.jobs}

    for item in plan:
        _ensure_keyframe(storage, item)

    # Resolve deterministic R2 replay before touching queue transport state. This is
    # deliberately earlier than resume reconciliation so a complete cached artifact
    # never requires a running Salad worker or a new transport submission.
    for item in plan:
        state = state_by_shot[item.shot_id]
        cached = cached_inference_response(storage, item.request)
        if cached is None:
            continue
        state.transport_status = "succeeded"
        state.response = cached
        _write_manifest(manifest_path, manifest)

    # Refresh known transports before deciding whether a resume needs a new submission.
    for item in plan:
        state = state_by_shot[item.shot_id]
        if state.transport_job_id is None:
            continue
        if state.transport_status == "succeeded" and state.response is not None:
            continue
        if state.transport_status in {"failed", "cancelled"}:
            continue
        try:
            snapshot = queue.get(state.transport_job_id)
        except QueueJobNotFoundError:
            state.transport_job_id = None
            state.transport_status = "unsubmitted"
            state.response = None
            _write_manifest(manifest_path, manifest)
            continue
        except TransientQueueError as exc:
            logger.warning(
                "Transient queue status read during Phase 8 resume "
                "shot_id=%s transport_job_id=%s; preserving persisted state: %s",
                item.shot_id,
                state.transport_job_id,
                exc,
            )
            continue
        _apply_snapshot(state, item, snapshot)
        _write_manifest(manifest_path, manifest)

    # Fan out every unresolved shot before entering the polling loop.
    for item in plan:
        state = state_by_shot[item.shot_id]
        should_submit = state.transport_status == "unsubmitted"
        should_retry = retry_terminal and state.transport_status in {"failed", "cancelled"}
        if not (should_submit or should_retry):
            continue
        snapshot = queue.submit(
            item.request,
            metadata={
                "application_job_id": item.request.job_id,
                "phase": "8.4",
                "shot_id": str(item.shot_id),
            },
        )
        state.transport_job_id = snapshot.id
        state.submission_count += 1
        _apply_snapshot(state, item, snapshot)
        _write_manifest(manifest_path, manifest)

    if not wait:
        return manifest, []

    transport_probe_ids = {
        state.transport_job_id
        for state in manifest.jobs
        if state.transport_job_id is not None
        and state.transport_status in {"pending", "running"}
    }
    dispatch_proven = any(
        state.transport_job_id in transport_probe_ids
        and state.transport_status == "running"
        for state in manifest.jobs
    )
    dispatch_deadline = time.monotonic() + dispatch_timeout_seconds
    deadline = time.monotonic() + timeout_seconds
    while any(
        state.transport_status in {"pending", "running"} for state in manifest.jobs
    ):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Video generation did not finish within {timeout_seconds} seconds")

        poll_had_transient = False
        for item in plan:
            state = state_by_shot[item.shot_id]
            if state.transport_status not in {"pending", "running"}:
                continue
            if state.transport_job_id is None:  # pragma: no cover - invariant protection
                raise RuntimeError(f"Shot {item.shot_id} has no transport job id")
            try:
                snapshot = queue.get(state.transport_job_id)
            except TransientQueueError as exc:
                poll_had_transient = True
                logger.warning(
                    "Transient queue status read during Phase 8 polling "
                    "shot_id=%s transport_job_id=%s; keeping job active: %s",
                    item.shot_id,
                    state.transport_job_id,
                    exc,
                )
                continue
            if snapshot.status in {QueueJobStatus.FAILED, QueueJobStatus.CANCELLED}:
                cached = cached_inference_response(storage, item.request)
                if cached is not None:
                    logger.warning(
                        "Accepting verified R2 replay after terminal LTX transport "
                        "status shot_id=%s transport_job_id=%s status=%s",
                        item.shot_id,
                        snapshot.id,
                        snapshot.status.value,
                    )
                    state.transport_job_id = snapshot.id
                    state.transport_status = "succeeded"
                    state.response = cached
                    _write_manifest(manifest_path, manifest)
                    continue
            _apply_snapshot(state, item, snapshot)
            if (
                state.transport_job_id in transport_probe_ids
                and state.transport_status in {"running", "succeeded", "failed"}
            ):
                dispatch_proven = True
            _write_manifest(manifest_path, manifest)

        if poll_had_transient and not dispatch_proven:
            # A control-plane outage is not evidence that queued jobs failed to dispatch.
            # Require a fresh continuous observation window before cancelling pending work.
            dispatch_deadline = time.monotonic() + dispatch_timeout_seconds

        if (
            not dispatch_proven
            and not poll_had_transient
            and time.monotonic() >= dispatch_deadline
        ):
            pending_probe_states = [
                state
                for state in manifest.jobs
                if state.transport_job_id in transport_probe_ids
                and state.transport_status == "pending"
            ]
            for state in pending_probe_states:
                transport_job_id = state.transport_job_id
                if transport_job_id is None:  # pragma: no cover - manifest invariant
                    raise RuntimeError(
                        f"Pending LTX transport lost its job id for shot {state.shot_id}"
                    )
                queue.cancel(transport_job_id)
                state.transport_status = "cancelled"
            _write_manifest(manifest_path, manifest)
            raise TimeoutError(
                f"LTX video generation did not dispatch any queued job within "
                f"{dispatch_timeout_seconds} seconds of observable queue health"
            )

        if any(
            state.transport_status in {"pending", "running"} for state in manifest.jobs
        ):
            time.sleep(poll_seconds)

    failed = [
        state for state in manifest.jobs if state.transport_status in {"failed", "cancelled"}
    ]
    if failed:
        details = "; ".join(terminal_failure_detail(state) for state in failed)
        raise VideoGenerationIncompleteError(
            f"Video generation has terminal transport failures: {details}. Rerun to resume."
        )

    clips = _download_completed_clips(
        plan,
        manifest,
        storage=storage,
        clips_dir=clips_dir,
        clip_uri_prefix=clip_uri_prefix,
    )
    return manifest, clips


def _validate_inputs(
    keyframes: list[StoryboardKeyframe],
    prompts: list[VideoPrompt],
    timings: list[ShotTiming],
) -> None:
    if not keyframes:
        raise ValueError("Video generation requires at least one storyboard keyframe")

    keyframe_ids = [item.shot_id for item in keyframes]
    expected_ids = list(range(1, len(keyframes) + 1))
    if keyframe_ids != expected_ids:
        raise ValueError("Video keyframes must have consecutive shot IDs starting at 1")
    if [item.shot_id for item in prompts] != keyframe_ids:
        raise ValueError("Video prompts must match keyframe shot IDs exactly and preserve order")
    if [item.shot_id for item in timings] != keyframe_ids:
        raise ValueError("Video timings must match keyframe shot IDs exactly and preserve order")

    previous_end: float | None = None
    for timing in timings:
        if timing.end_seconds <= timing.start_seconds:
            raise ValueError(f"Video shot {timing.shot_id} must have a positive duration")
        if previous_end is not None and abs(timing.start_seconds - previous_end) > 1e-6:
            raise ValueError("Video shot timings must form a contiguous timeline")
        previous_end = timing.end_seconds


def _load_or_create_manifest(
    plan: list[VideoGenerationPlanItem],
    manifest_path: Path,
    *,
    transport_route: str | None = None,
) -> VideoGenerationManifest:
    fingerprint = video_generation_run_fingerprint(plan)
    if manifest_path.is_file():
        manifest = VideoGenerationManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        same_plan = manifest.run_fingerprint == fingerprint
        route_changed = (
            transport_route is not None
            and manifest.transport_route != transport_route
        )
        if same_plan and not route_changed:
            _validate_manifest_against_plan(manifest, plan, fingerprint)
            return manifest
        if same_plan and route_changed:
            print(
                "Archived Phase 8 transport manifest because the queue route changed: "
                f"{manifest.transport_route!r} -> {transport_route!r}"
            )
            _archive_manifest(manifest_path, manifest.run_fingerprint)
        else:
            if any(
                state.transport_status in {"pending", "running"}
                for state in manifest.jobs
            ):
                raise ValueError(
                    "Existing video generation manifest belongs to a different input plan "
                    "and still contains active transports"
                )
            _archive_manifest(manifest_path, manifest.run_fingerprint)

    manifest = VideoGenerationManifest(
        run_fingerprint=fingerprint,
        transport_route=transport_route,
        jobs=[
            VideoGenerationJobState(
                shot_id=item.shot_id,
                application_job_id=item.request.job_id,
                request_sha256=item.request.fingerprint(),
            )
            for item in plan
        ],
    )
    _write_manifest(manifest_path, manifest)
    return manifest


def _validate_manifest_against_plan(
    manifest: VideoGenerationManifest,
    plan: list[VideoGenerationPlanItem],
    fingerprint: str,
) -> None:
    if manifest.run_fingerprint != fingerprint:
        raise ValueError(
            "Existing video generation manifest belongs to a different input plan; "
            "remove or archive it before starting a different run"
        )
    if [state.shot_id for state in manifest.jobs] != [item.shot_id for item in plan]:
        raise ValueError("Video generation manifest shot order does not match the current plan")

    for state, item in zip(manifest.jobs, plan, strict=True):
        if state.application_job_id != item.request.job_id:
            raise ValueError(f"Manifest application job mismatch for shot {item.shot_id}")
        if state.request_sha256 != item.request.fingerprint():
            raise ValueError(f"Manifest request fingerprint mismatch for shot {item.shot_id}")


def _archive_manifest(path: Path, run_fingerprint: str) -> Path:
    return archive_manifest(path, run_fingerprint, phase="generation")


def _write_manifest(path: Path, manifest: VideoGenerationManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )

    # Windows can transiently deny an atomic replace while another thread or process
    # has the destination open for reading. Phase 8's progress watcher intentionally
    # reads this manifest while generation is running, so tolerate short sharing
    # violations without giving up atomic persistence or losing resumability.
    replace_attempts = 100
    for attempt in range(replace_attempts):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == replace_attempts - 1:
                raise
            time.sleep(0.05)


def _ensure_keyframe(storage: ObjectStorage, item: VideoGenerationPlanItem) -> None:
    stored = storage.stat(item.input_key)
    if stored is not None:
        if stored.size_bytes != item.keyframe_path.stat().st_size:
            raise ValueError(f"Existing R2 keyframe size mismatch for shot {item.shot_id}")
        stored_sha256 = stored.metadata.get("artifact-sha256")
        if stored_sha256 is not None and stored_sha256 != item.keyframe_sha256:
            raise ValueError(f"Existing R2 keyframe SHA metadata mismatch for shot {item.shot_id}")
        return

    storage.upload(
        item.keyframe_path,
        item.input_key,
        content_type="image/png",
        metadata={
            "purpose": "phase8-keyframe",
            "shot-id": str(item.shot_id),
            "artifact-sha256": item.keyframe_sha256,
        },
    )


def _apply_snapshot(
    state: VideoGenerationJobState,
    item: VideoGenerationPlanItem,
    snapshot: QueueJobSnapshot,
) -> None:
    state.transport_job_id = snapshot.id
    state.transport_status = snapshot.status.value
    if snapshot.status in {QueueJobStatus.FAILED, QueueJobStatus.CANCELLED}:
        state.last_terminal_transport_job_id = snapshot.id
        state.last_terminal_payload = snapshot.provider_payload
    if snapshot.status is not QueueJobStatus.SUCCEEDED:
        state.response = None
        return

    response = _parse_success_response(snapshot.output)
    if response.job_id != item.request.job_id:
        raise ValueError(f"Worker returned the wrong application job for shot {item.shot_id}")
    if response.request_sha256 != item.request.fingerprint():
        raise ValueError(f"Worker returned the wrong request fingerprint for shot {item.shot_id}")
    if response.output.key != item.request.output.key:
        raise ValueError(f"Worker returned the wrong output key for shot {item.shot_id}")
    if response.output.content_type != "video/mp4":
        raise ValueError(f"Worker returned a non-MP4 artifact for shot {item.shot_id}")
    state.response = response


def _parse_success_response(value: object) -> InferenceJobResponse:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Succeeded queue job output must be a JSON object")
    return InferenceJobResponse.model_validate(value)


def _download_completed_clips(
    plan: list[VideoGenerationPlanItem],
    manifest: VideoGenerationManifest,
    *,
    storage: ObjectStorage,
    clips_dir: Path,
    clip_uri_prefix: str,
) -> list[VideoClip]:
    state_by_shot = {state.shot_id: state for state in manifest.jobs}
    clips_dir.mkdir(parents=True, exist_ok=True)
    clips: list[VideoClip] = []

    for item in plan:
        state = state_by_shot[item.shot_id]
        if state.response is None:
            raise RuntimeError(f"Shot {item.shot_id} succeeded without a validated worker response")
        artifact = state.response.output
        destination = clips_dir / f"shot_{item.shot_id:03d}.mp4"

        download_verified_mp4(
            storage, artifact, destination, shot_id=item.shot_id, kind="video"
        )

        uri = f"{clip_uri_prefix.rstrip('/')}/shot_{item.shot_id:03d}.mp4"
        clips.append(VideoClip(shot_id=item.shot_id, uri=uri))

    return clips
