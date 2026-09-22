from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_video_factory.compositor.media import probe_video
from ai_video_factory.domain import VideoClip
from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    InferenceJobResponse,
    ObjectInput,
    ObjectOutput,
)
from ai_video_factory.inference.ports import ObjectStorage
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.providers.inference_jobs import cached_inference_response
from ai_video_factory.providers.job_queue import (
    JobQueueClient,
    QueueJobNotFoundError,
    QueueJobSnapshot,
    QueueJobStatus,
    TransientQueueError,
)
from ai_video_factory.workers.realesrgan import (
    REALESRGAN_GENERATION_PROFILE,
    REALESRGAN_MODEL_NAME,
    REALESRGAN_TASK,
    realesrgan_application_job_id,
)

SOURCE_WIDTH = 1280
SOURCE_HEIGHT = 720
TARGET_WIDTH = 2560
TARGET_HEIGHT = 1440
UPSCALE_FPS = 24

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VideoUpscalePlanItem:
    shot_id: int
    source_path: Path
    source_sha256: str
    input_key: str
    request: InferenceJobRequest


class VideoUpscaleJobState(BaseModel):
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


class VideoUpscaleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    run_fingerprint: str
    jobs: list[VideoUpscaleJobState] = Field(min_length=1)


class VideoUpscaleIncompleteError(RuntimeError):
    """Raised when one or more Real-ESRGAN transports fail terminally."""


def build_video_upscale_plan(
    clips: list[VideoClip],
    *,
    clip_base_dir: Path,
    source_width: int = SOURCE_WIDTH,
    source_height: int = SOURCE_HEIGHT,
    target_width: int = TARGET_WIDTH,
    target_height: int = TARGET_HEIGHT,
    fps: int = UPSCALE_FPS,
    tile: int = 0,
    tile_pad: int = 10,
    pre_pad: int = 0,
    fp32: bool = False,
    crf: int = 12,
    preset: str = "medium",
) -> list[VideoUpscalePlanItem]:
    if not clips:
        raise ValueError("Video upscale requires at least one clip")
    shot_ids = [clip.shot_id for clip in clips]
    if shot_ids != list(range(1, len(clips) + 1)):
        raise ValueError("Upscale clips must have consecutive shot IDs starting at 1")
    if target_width != source_width * 2 or target_height != source_height * 2:
        raise ValueError("Production Real-ESRGAN target must be exactly 2x source dimensions")

    base = clip_base_dir.resolve()
    plan: list[VideoUpscalePlanItem] = []
    for clip in clips:
        source_path = (base / clip.uri).resolve()
        if not source_path.is_relative_to(base):
            raise ValueError(f"Shot {clip.shot_id} video URI escapes its base directory")
        if not source_path.is_file():
            raise FileNotFoundError(f"Phase 8 source clip not found: {source_path}")

        media = probe_video(source_path)
        if (media.width, media.height) != (source_width, source_height):
            raise ValueError(
                f"Shot {clip.shot_id} must be {source_width}x{source_height} before upscale; "
                f"found {media.width}x{media.height}"
            )
        if not math.isclose(media.fps, float(fps), rel_tol=0.0, abs_tol=1e-6):
            raise ValueError(f"Shot {clip.shot_id} must be {fps} fps before upscale")
        if media.audio_stream_count:
            raise ValueError(f"Shot {clip.shot_id} must be silent before upscale")
        frame_count = media.frame_count
        if frame_count is None:
            frame_count = round(media.duration_seconds * fps)
        if frame_count <= 0:
            raise ValueError(f"Shot {clip.shot_id} has no usable video frames")

        source_sha256 = sha256_file(source_path)
        job_id = realesrgan_application_job_id(
            shot_id=clip.shot_id,
            source_sha256=source_sha256,
            source_width=source_width,
            source_height=source_height,
            source_frame_count=frame_count,
            fps=fps,
            target_width=target_width,
            target_height=target_height,
            tile=tile,
            tile_pad=tile_pad,
            pre_pad=pre_pad,
            fp32=fp32,
            crf=crf,
            preset=preset,
        )
        input_key = f"phase8/realesrgan-input/{source_sha256}.mp4"
        request = InferenceJobRequest(
            job_id=job_id,
            task=REALESRGAN_TASK,
            inputs=[
                ObjectInput(
                    name="video",
                    key=input_key,
                    sha256=source_sha256,
                    content_type="video/mp4",
                )
            ],
            output=ObjectOutput(
                key=f"jobs/{job_id}/shot_{clip.shot_id:03d}.mp4",
                content_type="video/mp4",
            ),
            parameters={
                "generation_profile": REALESRGAN_GENERATION_PROFILE,
                "model_name": REALESRGAN_MODEL_NAME,
                "source_sha256": source_sha256,
                "source_width": source_width,
                "source_height": source_height,
                "source_frame_count": frame_count,
                "target_width": target_width,
                "target_height": target_height,
                "fps": fps,
                "tile": tile,
                "tile_pad": tile_pad,
                "pre_pad": pre_pad,
                "fp32": fp32,
                "encoder": "libx264",
                "crf": crf,
                "preset": preset,
                "pixel_format": "yuv420p",
            },
        )
        plan.append(
            VideoUpscalePlanItem(
                shot_id=clip.shot_id,
                source_path=source_path,
                source_sha256=source_sha256,
                input_key=input_key,
                request=request,
            )
        )
    return plan


def video_upscale_run_fingerprint(plan: list[VideoUpscalePlanItem]) -> str:
    if not plan:
        raise ValueError("Video upscale plan cannot be empty")
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


def run_video_upscale(
    plan: list[VideoUpscalePlanItem],
    *,
    queue: JobQueueClient,
    storage: ObjectStorage,
    manifest_path: Path,
    clips_dir: Path,
    clip_uri_prefix: str = "upscaled_clips",
    retry_terminal: bool = True,
    poll_seconds: float = 10.0,
    timeout_seconds: float = 7200.0,
    dispatch_timeout_seconds: float = 300.0,
) -> tuple[VideoUpscaleManifest, list[VideoClip]]:
    if not plan:
        raise ValueError("Video upscale plan cannot be empty")
    if poll_seconds <= 0 or timeout_seconds <= 0:
        raise ValueError("poll_seconds and timeout_seconds must be positive")
    if dispatch_timeout_seconds <= 0:
        raise ValueError("dispatch_timeout_seconds must be positive")

    manifest = _load_or_create_manifest(plan, manifest_path)
    state_by_shot = {state.shot_id: state for state in manifest.jobs}

    for item in plan:
        _ensure_source(storage, item)

    for item in plan:
        state = state_by_shot[item.shot_id]
        cached = cached_inference_response(storage, item.request)
        if cached is None:
            continue
        state.transport_status = "succeeded"
        state.response = cached
        _write_manifest(manifest_path, manifest)

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
                "Transient queue status read during Real-ESRGAN resume "
                "shot_id=%s transport_job_id=%s; preserving persisted state: %s",
                item.shot_id,
                state.transport_job_id,
                exc,
            )
            continue
        _apply_snapshot(state, item, snapshot)
        _write_manifest(manifest_path, manifest)

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
                "phase": "8-upscale",
                "shot_id": str(item.shot_id),
                "provider": "realesrgan",
            },
        )
        state.transport_job_id = snapshot.id
        state.submission_count += 1
        _apply_snapshot(state, item, snapshot)
        _write_manifest(manifest_path, manifest)

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
    while any(state.transport_status in {"pending", "running"} for state in manifest.jobs):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Real-ESRGAN upscale did not finish within {timeout_seconds} seconds"
            )

        poll_had_transient = False
        for item in plan:
            state = state_by_shot[item.shot_id]
            if state.transport_status not in {"pending", "running"}:
                continue
            if state.transport_job_id is None:
                raise RuntimeError(f"Shot {item.shot_id} has no transport job id")
            if _accept_cached_output(state, item, storage):
                _write_manifest(manifest_path, manifest)
                continue
            try:
                snapshot = queue.get(state.transport_job_id)
            except TransientQueueError as exc:
                poll_had_transient = True
                logger.warning(
                    "Transient queue status read during Real-ESRGAN polling "
                    "shot_id=%s transport_job_id=%s; keeping job active: %s",
                    item.shot_id,
                    state.transport_job_id,
                    exc,
                )
                continue
            if snapshot.status in {QueueJobStatus.FAILED, QueueJobStatus.CANCELLED} and (
                _accept_cached_output(state, item, storage)
            ):
                logger.warning(
                    "Accepting verified R2 replay after terminal Real-ESRGAN transport "
                    "status shot_id=%s transport_job_id=%s status=%s",
                    item.shot_id,
                    state.transport_job_id,
                    snapshot.status.value,
                )
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
                assert state.transport_job_id is not None
                queue.cancel(state.transport_job_id)
                state.transport_status = "cancelled"
            _write_manifest(manifest_path, manifest)
            raise TimeoutError(
                f"Real-ESRGAN upscale did not dispatch any queued job within "
                f"{dispatch_timeout_seconds} seconds of observable queue health"
            )

        if any(state.transport_status in {"pending", "running"} for state in manifest.jobs):
            time.sleep(poll_seconds)

    failed = [
        state for state in manifest.jobs if state.transport_status in {"failed", "cancelled"}
    ]
    if failed:
        detail = "; ".join(_terminal_failure_detail(state) for state in failed)
        raise VideoUpscaleIncompleteError(
            f"Real-ESRGAN upscale has terminal transport failures: {detail}. Rerun to resume."
        )

    clips = _download_completed_clips(
        plan,
        manifest,
        storage=storage,
        clips_dir=clips_dir,
        clip_uri_prefix=clip_uri_prefix,
    )
    return manifest, clips


def _accept_cached_output(
    state: VideoUpscaleJobState,
    item: VideoUpscalePlanItem,
    storage: ObjectStorage,
) -> bool:
    """Reconcile a durable output when Salad's transport state is stale."""

    cached = cached_inference_response(storage, item.request)
    if cached is None:
        return False
    state.transport_status = "succeeded"
    state.response = cached
    return True


def _load_or_create_manifest(
    plan: list[VideoUpscalePlanItem], manifest_path: Path
) -> VideoUpscaleManifest:
    fingerprint = video_upscale_run_fingerprint(plan)
    if manifest_path.is_file():
        manifest = VideoUpscaleManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        if manifest.run_fingerprint != fingerprint:
            if any(
                state.transport_status in {"pending", "running"}
                for state in manifest.jobs
            ):
                raise ValueError(
                    "Existing video upscale manifest belongs to a different input plan "
                    "and still contains active transports"
                )
            _archive_manifest(manifest_path, manifest.run_fingerprint)
            manifest = VideoUpscaleManifest(
                run_fingerprint=fingerprint,
                jobs=[
                    VideoUpscaleJobState(
                        shot_id=item.shot_id,
                        application_job_id=item.request.job_id,
                        request_sha256=item.request.fingerprint(),
                    )
                    for item in plan
                ],
            )
            _write_manifest(manifest_path, manifest)
            return manifest
        for state, item in zip(manifest.jobs, plan, strict=True):
            if state.shot_id != item.shot_id:
                raise ValueError("Video upscale manifest shot order does not match current plan")
            if state.application_job_id != item.request.job_id:
                raise ValueError(f"Upscale manifest job mismatch for shot {item.shot_id}")
            if state.request_sha256 != item.request.fingerprint():
                raise ValueError(f"Upscale manifest request mismatch for shot {item.shot_id}")
        return manifest

    manifest = VideoUpscaleManifest(
        run_fingerprint=fingerprint,
        jobs=[
            VideoUpscaleJobState(
                shot_id=item.shot_id,
                application_job_id=item.request.job_id,
                request_sha256=item.request.fingerprint(),
            )
            for item in plan
        ],
    )
    _write_manifest(manifest_path, manifest)
    return manifest


def _archive_manifest(path: Path, run_fingerprint: str) -> Path:
    raw = path.read_bytes()
    state_sha = hashlib.sha256(raw).hexdigest()[:12]
    archive = path.with_name(
        f"{path.stem}.archive-{run_fingerprint[:12]}-{state_sha}{path.suffix}"
    )
    if archive.exists():
        if archive.read_bytes() != raw:
            raise ValueError(f"Video upscale manifest archive collision: {archive}")
        path.unlink()
    else:
        os.replace(path, archive)
    print(f"Archived superseded video upscale manifest: {archive}")
    return archive


def _write_manifest(path: Path, manifest: VideoUpscaleManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _ensure_source(storage: ObjectStorage, item: VideoUpscalePlanItem) -> None:
    stored = storage.stat(item.input_key)
    if stored is not None:
        if stored.size_bytes != item.source_path.stat().st_size:
            raise ValueError(f"Existing R2 upscale source size mismatch for shot {item.shot_id}")
        source_sha = stored.metadata.get("artifact-sha256") or stored.metadata.get("sha256")
        if source_sha is None:
            raise ValueError(
                f"Existing R2 upscale source has no SHA metadata for shot {item.shot_id}"
            )
        if source_sha != item.source_sha256:
            raise ValueError(f"Existing R2 upscale source SHA mismatch for shot {item.shot_id}")
        return
    storage.upload(
        item.source_path,
        item.input_key,
        content_type="video/mp4",
        metadata={
            "purpose": "phase8-realesrgan-source",
            "shot-id": str(item.shot_id),
            "artifact-sha256": item.source_sha256,
            "sha256": item.source_sha256,
        },
    )


def _apply_snapshot(
    state: VideoUpscaleJobState,
    item: VideoUpscalePlanItem,
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
    value = snapshot.output
    if isinstance(value, str):
        value = json.loads(value)
    response = InferenceJobResponse.model_validate(value)
    if response.job_id != item.request.job_id:
        raise ValueError(f"Real-ESRGAN returned wrong job for shot {item.shot_id}")
    if response.request_sha256 != item.request.fingerprint():
        raise ValueError(f"Real-ESRGAN returned wrong request fingerprint for shot {item.shot_id}")
    if response.output.key != item.request.output.key:
        raise ValueError(f"Real-ESRGAN returned wrong output key for shot {item.shot_id}")
    state.response = response


def _terminal_failure_detail(state: VideoUpscaleJobState) -> str:
    parts = [
        f"shot {state.shot_id}={state.transport_status}",
        f"transport_job_id={state.transport_job_id or '<none>'}",
    ]
    if state.last_terminal_payload is not None:
        rendered = json.dumps(
            state.last_terminal_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if len(rendered) > 1800:
            rendered = rendered[:1797] + "..."
        parts.append(f"provider_payload={rendered}")
    return " ".join(parts)


def _download_completed_clips(
    plan: list[VideoUpscalePlanItem],
    manifest: VideoUpscaleManifest,
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
            raise RuntimeError(f"Shot {item.shot_id} upscale completed without response")
        artifact = state.response.output
        destination = clips_dir / f"shot_{item.shot_id:03d}.mp4"
        valid_local = (
            destination.is_file()
            and destination.stat().st_size == artifact.size_bytes
            and sha256_file(destination) == artifact.sha256
        )
        if not valid_local:
            storage.download(artifact.key, destination)
        if destination.stat().st_size != artifact.size_bytes:
            raise ValueError(f"Downloaded upscaled video size mismatch for shot {item.shot_id}")
        if sha256_file(destination) != artifact.sha256:
            raise ValueError(f"Downloaded upscaled video SHA mismatch for shot {item.shot_id}")

        media = probe_video(destination)
        source_media = probe_video(item.source_path)
        source_frames = source_media.frame_count or round(
            source_media.duration_seconds * UPSCALE_FPS
        )
        output_frames = media.frame_count or round(media.duration_seconds * UPSCALE_FPS)
        if (media.width, media.height) != (TARGET_WIDTH, TARGET_HEIGHT):
            raise ValueError(f"Upscaled shot {item.shot_id} is not 2560x1440")
        if not math.isclose(media.fps, UPSCALE_FPS, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError(f"Upscaled shot {item.shot_id} is not 24 fps")
        if output_frames != source_frames:
            raise ValueError(f"Upscaled shot {item.shot_id} changed frame count")
        if media.audio_stream_count:
            raise ValueError(f"Upscaled shot {item.shot_id} unexpectedly contains audio")
        clips.append(
            VideoClip(
                shot_id=item.shot_id,
                uri=f"{clip_uri_prefix.rstrip('/')}/shot_{item.shot_id:03d}.mp4",
            )
        )
    return clips
