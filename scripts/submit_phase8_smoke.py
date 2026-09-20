from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.gpu.contracts import GPUJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.gpu.ltx_jobs import ltx_video_application_job_id
from ai_video_factory.gpu.ltx_video import (
    LTX_GENERATION_PROFILE,
    LTX_VIDEO_TASK,
    ltx_num_frames_for_duration,
)
from ai_video_factory.gpu.storage import R2ObjectStorage, sha256_file

REQUIRED_ENV = (
    "SALAD_API_KEY",
    "SALAD_ORGANIZATION",
    "SALAD_PROJECT",
    "R2_ENDPOINT_URL",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def _event(name: str, **fields: object) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    suffix = f" {details}" if details else ""
    print(f"{name}{suffix}", flush=True)


def _environment() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    return {name: os.environ[name] for name in REQUIRED_ENV}


def _salad_request(
    url: str,
    api_key: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Salad-Api-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ai-video-factory-phase8/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Salad API returned HTTP {exc.code}: {detail}") from exc


def _normalized_queue_output(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"queue output is not JSON: {value!r}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"queue output has an unexpected type: {type(value).__name__}")
    return value


def _read_models[ModelT](path: Path, model_type: type[ModelT]) -> list[ModelT]:
    if not path.is_file():
        raise SystemExit(f"Required JSON file not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise SystemExit(f"JSON file must contain an array: {path}")
    return [model_type.model_validate(item) for item in value]  # type: ignore[attr-defined]


def _one_by_id[ModelT](items: list[ModelT], identifier: int, attribute: str) -> ModelT:
    matches = [item for item in items if getattr(item, attribute) == identifier]
    if len(matches) != 1:
        item_name = type(items[0]).__name__ if items else "item"
        raise SystemExit(
            f"Expected exactly one {item_name} with {attribute}={identifier}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _ffprobe(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise RuntimeError(
            "ffprobe is required for Phase 8 smoke validation; refusing unverified success"
        )
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit and verify one real Phase 8 LTX-2.5 video job through Salad/R2."
    )
    parser.add_argument("--shot-id", type=int, default=1)
    parser.add_argument(
        "--queue-name",
        default=os.getenv("SALAD_QUEUE_NAME", "ai-video-factory-jobs"),
    )
    parser.add_argument(
        "--keyframes",
        type=Path,
        default=Path("data/output/phase6/storyboard_keyframes.json"),
    )
    parser.add_argument(
        "--prompts",
        type=Path,
        default=Path("data/output/phase8/video_prompts.json"),
    )
    parser.add_argument(
        "--timings",
        type=Path,
        default=Path("data/output/phase5/shot_timings.json"),
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument(
        "--pending-timeout-seconds",
        type=int,
        default=0,
        help=(
            "Optional maximum time a submitted job may remain pending. "
            "Zero disables the pending-only timeout."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/output/phase8/cloud"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.shot_id < 1:
        raise SystemExit("--shot-id must be >= 1")
    if args.pending_timeout_seconds < 0:
        raise SystemExit("--pending-timeout-seconds must be >= 0")

    _event("SMOKE_START", shot_id=args.shot_id, queue=args.queue_name)
    _event("REQUEST_BUILD_START")
    environment = _environment()
    keyframes = _read_models(args.keyframes, StoryboardKeyframe)
    prompts = _read_models(args.prompts, VideoPrompt)
    timings = _read_models(args.timings, ShotTiming)

    keyframe = _one_by_id(keyframes, args.shot_id, "shot_id")
    prompt = _one_by_id(prompts, args.shot_id, "shot_id")
    timing = _one_by_id(timings, args.shot_id, "shot_id")

    keyframe_path = (args.keyframes.parent / keyframe.uri).resolve()
    if not keyframe_path.is_file():
        raise SystemExit(f"Storyboard keyframe file not found: {keyframe_path}")

    duration_seconds = timing.end_seconds - timing.start_seconds
    num_frames = ltx_num_frames_for_duration(duration_seconds, fps=args.fps)
    seed = args.seed if args.seed is not None else 42 + args.shot_id
    keyframe_sha256 = sha256_file(keyframe_path)
    job_id = ltx_video_application_job_id(
        shot_id=args.shot_id,
        prompt=prompt.prompt,
        keyframe_sha256=keyframe_sha256,
        seed=seed,
        width=args.width,
        height=args.height,
        fps=args.fps,
        num_frames=num_frames,
    )
    input_key = f"phase8/keyframes/{keyframe_sha256}.png"
    output_key = f"jobs/{job_id}/shot_{args.shot_id:03d}.mp4"
    job = GPUJobRequest(
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
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
            "num_frames": num_frames,
        },
    )
    body = {
        "input": job.model_dump(mode="json", exclude_none=True),
        "metadata": {
            "application_job_id": job_id,
            "phase": "8.3",
            "shot_id": str(args.shot_id),
        },
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    request_path = args.output_dir / f"job-request-{job_id}.json"
    response_path = args.output_dir / f"queue-response-{job_id}.json"
    request_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    _event("REQUEST_BUILD_DONE", application_job_id=job_id, request=request_path)

    storage = R2ObjectStorage.create(
        endpoint_url=environment["R2_ENDPOINT_URL"],
        bucket=environment["R2_BUCKET"],
        access_key_id=environment["R2_ACCESS_KEY_ID"],
        secret_access_key=environment["R2_SECRET_ACCESS_KEY"],
    )
    _event("R2_UPLOAD_START", input_key=input_key)
    storage.upload(
        keyframe_path,
        input_key,
        content_type="image/png",
        metadata={
            "purpose": "phase8-keyframe",
            "shot-id": str(args.shot_id),
            "artifact-sha256": keyframe_sha256,
        },
    )
    _event("R2_UPLOAD_DONE", input_key=input_key)

    queue_url = (
        "https://api.salad.com/api/public/organizations/"
        f"{environment['SALAD_ORGANIZATION']}/projects/{environment['SALAD_PROJECT']}"
        f"/queues/{args.queue_name}"
    )
    base_url = f"{queue_url}/jobs"
    _event("QUEUE_PREFLIGHT_START", queue=args.queue_name)
    queue_state = _salad_request(queue_url, environment["SALAD_API_KEY"])
    container_groups = queue_state.get("container_groups") or []
    group_names = ",".join(
        str(group.get("name", "?"))
        for group in container_groups
        if isinstance(group, dict)
    )
    _event(
        "QUEUE_PREFLIGHT_DONE",
        queue=args.queue_name,
        current_queue_length=queue_state.get("current_queue_length", "?"),
        container_groups=group_names or "-",
    )

    _event("QUEUE_SUBMIT_START", queue=args.queue_name)
    created = _salad_request(
        base_url,
        environment["SALAD_API_KEY"],
        method="POST",
        body=body,
    )
    response_path.write_text(json.dumps(created, indent=2) + "\n", encoding="utf-8")
    _event(
        "QUEUE_SUBMIT_DONE",
        salad_job_id=created["id"],
        status=created.get("status", "?"),
    )

    print(f"application_job_id={job_id}", flush=True)
    print(f"salad_job_id={created['id']}", flush=True)
    print(f"shot_id={args.shot_id}", flush=True)
    print(f"duration_seconds={duration_seconds:.3f}", flush=True)
    print(f"num_frames={num_frames}", flush=True)
    print(f"seed={seed}", flush=True)
    print(f"input_key={input_key}", flush=True)
    print(f"output_key={output_key}", flush=True)
    print(request_path, flush=True)
    print(response_path, flush=True)

    if not args.wait:
        _event("SMOKE_DONE", status="submitted", salad_job_id=created["id"])
        return

    job_url = f"{base_url}/{created['id']}"
    deadline = time.monotonic() + args.timeout_seconds
    current = created
    pending_since = time.monotonic() if current.get("status") == "pending" else None
    _event(
        "QUEUE_WAIT_START",
        salad_job_id=created["id"],
        timeout_seconds=args.timeout_seconds,
        pending_timeout_seconds=args.pending_timeout_seconds,
    )
    while current["status"] not in {"succeeded", "failed", "cancelled"}:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"job did not finish within {args.timeout_seconds} seconds")
        time.sleep(args.poll_seconds)
        current = _salad_request(job_url, environment["SALAD_API_KEY"])
        status = str(current["status"])
        _event("QUEUE_WAIT_STATUS", salad_job_id=created["id"], status=status)
        if status == "pending":
            if pending_since is None:
                pending_since = time.monotonic()
            pending_elapsed = time.monotonic() - pending_since
            if (
                args.pending_timeout_seconds > 0
                and pending_elapsed >= args.pending_timeout_seconds
            ):
                raise TimeoutError(
                    "job remained pending for "
                    f"{args.pending_timeout_seconds} seconds; verify queue attachment/routing"
                )
        else:
            pending_since = None

    response_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    if current["status"] != "succeeded":
        terminal_detail = json.dumps(current, ensure_ascii=False, sort_keys=True)
        raise RuntimeError(
            f"Salad job ended with status {current['status']}: {terminal_detail}"
        )

    output = _normalized_queue_output(current.get("output"))
    if output.get("status") != "succeeded":
        raise RuntimeError(f"worker returned a terminal rejection: {output}")
    artifact = output.get("output", {})
    if artifact.get("key") != output_key:
        raise RuntimeError(f"worker returned an unexpected output key: {artifact}")
    invalid_content_type = artifact.get("content_type") != "video/mp4"
    invalid_size = int(artifact.get("size_bytes", 0)) <= 0
    if invalid_content_type or invalid_size:
        raise RuntimeError(f"worker returned invalid MP4 metadata: {artifact}")

    destination = args.output_dir / f"shot_{args.shot_id:03d}.mp4"
    _event("ARTIFACT_DOWNLOAD_START", output_key=output_key)
    storage.download(output_key, destination)
    _event("ARTIFACT_DOWNLOAD_DONE", destination=destination)
    downloaded_sha256 = sha256_file(destination)
    if downloaded_sha256 != artifact.get("sha256"):
        raise RuntimeError(
            "downloaded MP4 sha256 does not match worker metadata: "
            f"{downloaded_sha256} != {artifact.get('sha256')}"
        )

    probe = _ffprobe(destination)
    probe_path = args.output_dir / f"shot_{args.shot_id:03d}-ffprobe.json"
    probe_path.write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")
    video_streams = [
        item for item in probe.get("streams", []) if item.get("codec_type") == "video"
    ]
    audio_streams = [
        item for item in probe.get("streams", []) if item.get("codec_type") == "audio"
    ]
    if len(video_streams) != 1:
        raise RuntimeError(f"expected exactly one video stream, got {len(video_streams)}")
    if audio_streams:
        raise RuntimeError("Phase 8 MP4 unexpectedly contains an audio stream")
    video_stream = video_streams[0]
    if (
        int(video_stream.get("width", 0)) != args.width
        or int(video_stream.get("height", 0)) != args.height
    ):
        raise RuntimeError(
            "Phase 8 MP4 dimensions do not match the requested contract: "
            f"{video_stream.get('width')}x{video_stream.get('height')} "
            f"!= {args.width}x{args.height}"
        )
    rate = str(
        video_stream.get("avg_frame_rate")
        or video_stream.get("r_frame_rate")
        or ""
    )
    if "/" not in rate:
        raise RuntimeError(f"Phase 8 MP4 has invalid frame rate metadata: {rate!r}")
    numerator, denominator = rate.split("/", maxsplit=1)
    actual_fps = float(numerator) / float(denominator)
    if abs(actual_fps - args.fps) > 1e-6:
        raise RuntimeError(
            f"Phase 8 MP4 fps {actual_fps} does not match requested {args.fps}"
        )
    print(probe_path, flush=True)

    print(f"downloaded={destination.resolve()}", flush=True)
    print(f"sha256={downloaded_sha256}", flush=True)
    print(f"replayed={bool(output.get('replayed'))}", flush=True)
    print(f"attempt_count={output.get('attempt_count')}", flush=True)
    _event("SMOKE_DONE", status="succeeded", salad_job_id=created["id"])
    print("Phase 8.3 real video smoke test: OK", flush=True)


if __name__ == "__main__":
    main()
