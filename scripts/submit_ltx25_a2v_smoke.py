from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.gpu.storage import R2ObjectStorage
from ai_video_factory.workers.ltx25.a2v import (
    LTX_A2V_DEFAULT_PROMPT,
    LTX_A2V_GENERATION_PROFILE,
    LTX_A2V_TASK,
)
from ai_video_factory.workers.ltx25.jobs import ltx_a2v_application_job_id

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
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"{name}{' ' if suffix else ''}{suffix}", flush=True)


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
            "User-Agent": "ai-video-factory-ltx25-a2v/0.1",
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
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError(f"queue output has unexpected type: {type(value).__name__}")
    return value


def _ffprobe(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise RuntimeError("ffprobe is required for A2V smoke validation")
    completed = subprocess.run(
        [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def _duration(probe: dict[str, Any]) -> float:
    try:
        return float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("ffprobe did not report a valid media duration") from exc


def _assert_audio_not_silent(path: Path) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("ffmpeg is required for A2V audio validation")
    completed = subprocess.run(
        [
            executable,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-vn",
            "-af",
            "volumedetect",
            "-f",
            "null",
            os.devnull,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = completed.stderr
    match = re.search(r"mean_volume:\s+([^ ]+) dB", output)
    if completed.returncode != 0 or match is None or match.group(1) == "-inf":
        raise RuntimeError("A2V output audio is missing, undecodable, or silent")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit and validate one real LTX-2.5 image+speech A2V job on Salad."
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--avatar-image", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--prompt", default=LTX_A2V_DEFAULT_PROMPT)
    parser.add_argument("--segment-id", default="smoke-001")
    parser.add_argument("--queue-name", default=os.getenv(
        "SALAD_LTX25_QUEUE_NAME", "ai-video-factory-ltx25-jobs-v2"
    ))
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--seed", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--pending-timeout-seconds", type=int, default=300)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/output/deployment-validation/ltx25-a2v"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(args.env_file, override=False)
    environment = _environment()
    avatar = args.avatar_image.resolve()
    audio = args.audio.resolve()
    if not avatar.is_file():
        raise SystemExit(f"Avatar image not found: {avatar}")
    if avatar.suffix.lower() != ".png":
        raise SystemExit("--avatar-image must currently be a PNG for the smoke upload contract")
    if not audio.is_file():
        raise SystemExit(f"Audio clip not found: {audio}")

    avatar_sha = sha256_file(avatar)
    audio_sha = sha256_file(audio)
    job_id = ltx_a2v_application_job_id(
        segment_id=args.segment_id,
        prompt=args.prompt,
        avatar_image_sha256=avatar_sha,
        audio_sha256=audio_sha,
        seed=args.seed,
        width=args.width,
        height=args.height,
        fps=args.fps,
    )
    image_key = f"ltx25-a2v/inputs/images/{avatar_sha}{avatar.suffix.lower()}"
    audio_key = f"ltx25-a2v/inputs/audio/{audio_sha}{audio.suffix.lower()}"
    video_key = f"jobs/{job_id}/video.mp4"
    metadata_key = f"jobs/{job_id}/metadata.json"
    job = InferenceJobRequest(
        job_id=job_id,
        task=LTX_A2V_TASK,
        inputs=[
            ObjectInput(
                name="avatar_image",
                key=image_key,
                sha256=avatar_sha,
                content_type="image/png",
            ),
            ObjectInput(
                name="audio",
                key=audio_key,
                sha256=audio_sha,
            ),
        ],
        output=ObjectOutput(key=video_key, content_type="video/mp4"),
        sidecar_outputs={
            "metadata": ObjectOutput(key=metadata_key, content_type="application/json")
        },
        parameters={
            "generation_profile": LTX_A2V_GENERATION_PROFILE,
            "prompt": args.prompt,
            "seed": args.seed,
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
        },
    )
    body = {
        "input": job.model_dump(mode="json", exclude_none=True),
        "metadata": {
            "application_job_id": job_id,
            "capability": "ltx25-a2v",
            "segment_id": args.segment_id,
        },
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    request_path = args.output_dir / f"job-request-{job_id}.json"
    response_path = args.output_dir / f"queue-response-{job_id}.json"
    request_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

    storage = R2ObjectStorage.create(
        endpoint_url=environment["R2_ENDPOINT_URL"],
        bucket=environment["R2_BUCKET"],
        access_key_id=environment["R2_ACCESS_KEY_ID"],
        secret_access_key=environment["R2_SECRET_ACCESS_KEY"],
    )
    _event("A2V_INPUT_UPLOAD_START", job_id=job_id)
    storage.upload(
        avatar,
        image_key,
        content_type="image/png",
        metadata={"purpose": "ltx25-a2v-avatar", "sha256": avatar_sha},
    )
    storage.upload(
        audio,
        audio_key,
        content_type="application/octet-stream",
        metadata={"purpose": "ltx25-a2v-audio", "sha256": audio_sha},
    )
    _event("A2V_INPUT_UPLOAD_DONE", job_id=job_id)

    queue_url = (
        "https://api.salad.com/api/public/organizations/"
        f"{environment['SALAD_ORGANIZATION']}/projects/{environment['SALAD_PROJECT']}"
        f"/queues/{args.queue_name}"
    )
    base_url = f"{queue_url}/jobs"
    created = _salad_request(
        base_url,
        environment["SALAD_API_KEY"],
        method="POST",
        body=body,
    )
    salad_job_id = str(created["id"])
    _event("A2V_QUEUE_SUBMITTED", salad_job_id=salad_job_id, application_job_id=job_id)

    deadline = time.monotonic() + args.timeout_seconds
    pending_since = time.monotonic() if created.get("status") == "pending" else None
    current = created
    job_url = f"{base_url}/{salad_job_id}"
    while current.get("status") not in {"succeeded", "failed", "cancelled"}:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"A2V job exceeded {args.timeout_seconds}s total timeout")
        time.sleep(args.poll_seconds)
        current = _salad_request(job_url, environment["SALAD_API_KEY"])
        status = str(current.get("status"))
        _event("A2V_QUEUE_STATUS", salad_job_id=salad_job_id, status=status)
        if status == "pending":
            pending_since = pending_since or time.monotonic()
            if time.monotonic() - pending_since >= args.pending_timeout_seconds:
                raise TimeoutError(
                    f"A2V job remained pending for {args.pending_timeout_seconds}s"
                )
        else:
            pending_since = None

    response_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    if current.get("status") != "succeeded":
        raise RuntimeError(f"A2V Salad job ended with status {current.get('status')}: {current}")

    output = _normalized_queue_output(current.get("output"))
    if output.get("status") != "succeeded":
        raise RuntimeError(f"A2V worker returned terminal rejection: {output}")
    if output.get("output", {}).get("key") != video_key:
        raise RuntimeError(f"A2V worker returned unexpected primary artifact: {output}")
    sidecars = output.get("sidecar_outputs") or {}
    if sidecars.get("metadata", {}).get("key") != metadata_key:
        raise RuntimeError(f"A2V worker returned unexpected metadata sidecar: {sidecars}")

    video_path = args.output_dir / "avatar_segment.mp4"
    metadata_path = args.output_dir / "metadata.json"
    storage.download(video_key, video_path)
    storage.download(metadata_key, metadata_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    probe = _ffprobe(video_path)
    probe_path = args.output_dir / "ffprobe.json"
    probe_path.write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")

    video_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    audio_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    if len(video_streams) != 1:
        raise RuntimeError(f"expected one video stream; found {len(video_streams)}")
    if len(audio_streams) != 1:
        raise RuntimeError(f"expected one audio stream; found {len(audio_streams)}")
    video = video_streams[0]
    if (int(video.get("width", 0)), int(video.get("height", 0))) != (
        args.width,
        args.height,
    ):
        raise RuntimeError(
            f"A2V dimensions {video.get('width')}x{video.get('height')} "
            f"!= {args.width}x{args.height}"
        )
    rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    numerator, denominator = rate.split("/", maxsplit=1)
    actual_fps = float(numerator) / float(denominator)
    if abs(actual_fps - args.fps) > 1e-3:
        raise RuntimeError(f"A2V fps {actual_fps} != requested {args.fps}")
    _assert_audio_not_silent(video_path)

    input_probe = _ffprobe(audio)
    input_duration = _duration(input_probe)
    output_duration = _duration(probe)
    tolerance = max(0.5, 8.0 / args.fps)
    if abs(output_duration - input_duration) > tolerance:
        raise RuntimeError(
            "A2V output duration diverges from input audio: "
            f"input={input_duration:.3f}s output={output_duration:.3f}s "
            f"tolerance={tolerance:.3f}s"
        )

    _event(
        "A2V_SMOKE_METRIC",
        input_audio_seconds=f"{input_duration:.3f}",
        output_video_seconds=f"{output_duration:.3f}",
        fps=f"{actual_fps:.3f}",
        width=args.width,
        height=args.height,
        num_frames=metadata.get("num_frames"),
        model_load_seconds=metadata.get("model_load_seconds"),
        inference_seconds=metadata.get("inference_seconds"),
        encode_seconds=metadata.get("encode_seconds"),
        total_elapsed_seconds=metadata.get("total_elapsed_seconds"),
        peak_vram_bytes=metadata.get("peak_vram_bytes"),
        video_sha256=sha256_file(video_path),
    )
    print(f"video={video_path.resolve()}", flush=True)
    print(f"metadata={metadata_path.resolve()}", flush=True)
    print(f"ffprobe={probe_path.resolve()}", flush=True)
    print("LTX 2.5 A2V real Salad smoke: OK", flush=True)


if __name__ == "__main__":
    main()
