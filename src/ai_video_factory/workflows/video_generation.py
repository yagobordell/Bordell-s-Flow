from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoClip, VideoPrompt
from ai_video_factory.gpu.contracts import GPUJobRequest, GPUJobResponse, ObjectInput, ObjectOutput
from ai_video_factory.gpu.ltx_jobs import ltx_video_application_job_id
from ai_video_factory.gpu.ltx_video import (
    LTX_GENERATION_PROFILE,
    LTX_VIDEO_TASK,
    ltx_num_frames_for_duration,
)
from ai_video_factory.gpu.ports import ObjectStorage
from ai_video_factory.gpu.storage import sha256_file
from ai_video_factory.providers.job_queue import JobQueueClient, QueueJobSnapshot, QueueJobStatus


@dataclass(frozen=True, slots=True)
class VideoGenerationPlanItem:
    shot_id: int
    keyframe_path: Path
    keyframe_sha256: str
    input_key: str
    request: GPUJobRequest


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
    response: GPUJobResponse | None = None


class VideoGenerationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    run_fingerprint: str
    jobs: list[VideoGenerationJobState] = Field(min_length=1)


class VideoGenerationIncompleteError(RuntimeError):
    """Raised when one or more transport jobs reached a terminal non-success state."""


def build_video_generation_plan(
    keyframes: list[StoryboardKeyframe],
    prompts: list[VideoPrompt],
    timings: list[ShotTiming],
    *,
    keyframe_base_dir: Path,
    width: int = 768,
    height: int = 1280,
    fps: int = 24,
    seed_base: int = 42,
) -> list[VideoGenerationPlanItem]:
    """Build deterministic LTX queue requests for every canonical shot."""

    _validate_inputs(keyframes, prompts, timings)
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("Video generation dimensions and fps must be positive")

    base_dir = keyframe_base_dir.resolve()
    plan: list[VideoGenerationPlanItem] = []

    for keyframe, prompt, timing in zip(keyframes, prompts, timings, strict=True):
        keyframe_path = (base_dir / keyframe.uri).resolve()
        if not keyframe_path.is_relative_to(base_dir):
            raise ValueError(f"Shot {keyframe.shot_id} keyframe URI escapes its base directory")
        if not keyframe_path.is_file():
            raise FileNotFoundError(f"Storyboard keyframe file not found: {keyframe_path}")

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
        request = GPUJobRequest(
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
) -> tuple[VideoGenerationManifest, list[VideoClip]]:
    """Fan out LTX jobs, persist progress, resume transports and fan in canonical clips."""

    if poll_seconds <= 0 or timeout_seconds <= 0:
        raise ValueError("poll_seconds and timeout_seconds must be positive")
    if not plan:
        raise ValueError("Video generation plan cannot be empty")

    manifest = _load_or_create_manifest(plan, manifest_path)
    state_by_shot = {state.shot_id: state for state in manifest.jobs}

    for item in plan:
        _ensure_keyframe(storage, item)

    # Refresh known transports before deciding whether a resume needs a new submission.
    for item in plan:
        state = state_by_shot[item.shot_id]
        if state.transport_job_id is None:
            continue
        if state.transport_status == "succeeded" and state.response is not None:
            continue
        if state.transport_status in {"failed", "cancelled"}:
            continue
        snapshot = queue.get(state.transport_job_id)
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

    deadline = time.monotonic() + timeout_seconds
    while any(
        state.transport_status in {"pending", "running"} for state in manifest.jobs
    ):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Video generation did not finish within {timeout_seconds} seconds")

        for item in plan:
            state = state_by_shot[item.shot_id]
            if state.transport_status not in {"pending", "running"}:
                continue
            if state.transport_job_id is None:  # pragma: no cover - invariant protection
                raise RuntimeError(f"Shot {item.shot_id} has no transport job id")
            snapshot = queue.get(state.transport_job_id)
            _apply_snapshot(state, item, snapshot)
            _write_manifest(manifest_path, manifest)

        if any(
            state.transport_status in {"pending", "running"} for state in manifest.jobs
        ):
            time.sleep(poll_seconds)

    failed = [
        state for state in manifest.jobs if state.transport_status in {"failed", "cancelled"}
    ]
    if failed:
        details = ", ".join(f"shot {state.shot_id}={state.transport_status}" for state in failed)
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
    plan: list[VideoGenerationPlanItem], manifest_path: Path
) -> VideoGenerationManifest:
    fingerprint = video_generation_run_fingerprint(plan)
    if manifest_path.is_file():
        manifest = VideoGenerationManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        _validate_manifest_against_plan(manifest, plan, fingerprint)
        return manifest

    manifest = VideoGenerationManifest(
        run_fingerprint=fingerprint,
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


def _write_manifest(path: Path, manifest: VideoGenerationManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


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


def _parse_success_response(value: object) -> GPUJobResponse:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Succeeded queue job output must be a JSON object")
    return GPUJobResponse.model_validate(value)


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

        valid_local = (
            destination.is_file()
            and destination.stat().st_size == artifact.size_bytes
            and sha256_file(destination) == artifact.sha256
        )
        if not valid_local:
            storage.download(artifact.key, destination)

        if destination.stat().st_size != artifact.size_bytes:
            raise ValueError(f"Downloaded video size mismatch for shot {item.shot_id}")
        if sha256_file(destination) != artifact.sha256:
            raise ValueError(f"Downloaded video SHA mismatch for shot {item.shot_id}")

        uri = f"{clip_uri_prefix.rstrip('/')}/shot_{item.shot_id:03d}.mp4"
        clips.append(VideoClip(shot_id=item.shot_id, uri=uri))

    return clips
