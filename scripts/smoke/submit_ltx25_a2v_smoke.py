from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.storage import R2ObjectStorage, sha256_file
from ai_video_factory.workers.ltx25 import (
    LTX_A2V_DEFAULT_PROMPT,
    LTX_A2V_DEV_GENERATION_PROFILE,
    LTX_A2V_GENERATION_PROFILE,
    LTX_A2V_TASK,
    ltx_a2v_application_job_id,
)

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
    print(f"{name}{' ' + details if details else ''}", flush=True)


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
            "User-Agent": "ai-video-factory-ltx25-a2v-smoke/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload_bytes = response.read()
            if not payload_bytes:
                return {}
            text = payload_bytes.decode("utf-8")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Salad API returned HTTP {exc.code}: {detail}") from exc


def _queue_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("items")
    if raw is None:
        raw = payload.get("jobs")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _cancel_stale_pending_application_jobs(
    *,
    base_url: str,
    api_key: str,
    application_job_id: str,
) -> None:
    for page in range(1, 101):
        payload = _salad_request(
            f"{base_url}?page={page}&page_size=25",
            api_key,
        )
        items = _queue_items(payload)
        for item in items:
            metadata = item.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if metadata.get("application_job_id") != application_job_id:
                continue
            status = str(item.get("status") or "")
            transport_job_id = str(item.get("id") or "")
            if status == "running":
                raise RuntimeError(
                    "Refusing to submit a duplicate A2V transport because the same "
                    f"application job is already running: {transport_job_id}"
                )
            if status != "pending" or not transport_job_id:
                continue
            _event(
                "A2V_STALE_PENDING_CANCEL_START",
                salad_job_id=transport_job_id,
                application_job_id=application_job_id,
            )
            _cancel_pending_transport(
                f"{base_url}/{transport_job_id}",
                api_key,
                transport_job_id,
                strict=True,
            )
            _event(
                "A2V_STALE_PENDING_CANCEL_DONE",
                salad_job_id=transport_job_id,
            )
        if len(items) < 25:
            return
    raise RuntimeError("A2V stale-job scan exceeded 100 queue pages")


def _reallocate_single_group_instance(
    *,
    organization: str,
    project: str,
    group_name: str,
    api_key: str,
) -> str:
    group_url = (
        "https://api.salad.com/api/public/organizations/"
        f"{organization}/projects/{project}/containers/{group_name}"
    )
    payload = _salad_request(f"{group_url}/instances", api_key)
    raw_instances = payload.get("instances")
    if raw_instances is None:
        raw_instances = payload.get("items")
    instances = [
        item for item in (raw_instances or [])
        if isinstance(item, dict)
    ]
    if len(instances) != 1:
        raise RuntimeError(
            "A2V pending recovery requires exactly one Salad instance; "
            f"found {len(instances)}"
        )
    instance_id = str(instances[0].get("id") or "")
    if not instance_id:
        raise RuntimeError("A2V pending recovery could not resolve the Salad instance id")
    _salad_request(
        f"{group_url}/instances/{instance_id}/reallocate",
        api_key,
        method="POST",
    )
    return instance_id


def _cancel_pending_transport(
    job_url: str,
    api_key: str,
    salad_job_id: str,
    *,
    strict: bool = False,
) -> None:
    try:
        _salad_request(job_url, api_key, method="DELETE")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                current = _salad_request(job_url, api_key)
            except RuntimeError as exc:
                if "HTTP 404" in str(exc):
                    _event("A2V_PENDING_CANCEL_DONE", salad_job_id=salad_job_id)
                    return
                raise
            if str(current.get("status")) == "cancelled":
                _event("A2V_PENDING_CANCEL_DONE", salad_job_id=salad_job_id)
                return
        raise TimeoutError(
            f"Salad did not confirm cancellation of pending job {salad_job_id}"
        )
    except Exception as exc:
        _event(
            "A2V_PENDING_CANCEL_FAILED",
            salad_job_id=salad_job_id,
            error=repr(exc),
        )
        if strict:
            raise


def _write_default_avatar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1280, 720), (48, 55, 68))
    draw = ImageDraw.Draw(image)
    draw.ellipse((460, 100, 820, 460), fill=(205, 168, 138))
    draw.pieslice((445, 65, 835, 420), 180, 360, fill=(65, 48, 40))
    draw.ellipse((545, 240, 575, 265), fill=(35, 30, 28))
    draw.ellipse((705, 240, 735, 265), fill=(35, 30, 28))
    draw.arc((560, 270, 720, 380), start=20, end=160, fill=(120, 55, 55), width=6)
    draw.rounded_rectangle((400, 445, 880, 720), radius=90, fill=(55, 86, 116))
    image.save(path, format="PNG")


def _ffprobe(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise RuntimeError("ffprobe is required for A2V smoke validation")
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


def _duration(probe: dict[str, Any]) -> float:
    value = (probe.get("format") or {}).get("duration")
    duration = float(value)
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"invalid media duration: {value!r}")
    return duration


def _validate_decodable_video(path: Path) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("ffmpeg is required to validate the decoded A2V video")
    completed = subprocess.run(
        [executable, "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"generated video failed decode validation: {completed.stderr[-1200:]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit and verify one real LTX-2.5 image+speech A2V job through Salad/R2."
    )
    parser.add_argument("--avatar-image", type=Path)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--prompt", default=LTX_A2V_DEFAULT_PROMPT)
    parser.add_argument("--segment-id", default="smoke-001")
    parser.add_argument("--profile", choices=("fast", "dev"), default="fast")
    parser.add_argument("--max-generation-seconds", type=float, default=0.0)
    parser.add_argument(
        "--queue-name",
        default=os.getenv("SALAD_LTX25_QUEUE_NAME", "ai-video-factory-ltx25-jobs-v2"),
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--seed", type=int, default=4242)
    parser.add_argument("--timeout-seconds", type=int, default=10800)
    parser.add_argument("--pending-timeout-seconds", type=int, default=180)
    parser.add_argument("--max-pending-reallocations", type=int, default=1)
    parser.add_argument("--post-reallocation-pending-seconds", type=int, default=1200)
    parser.add_argument(
        "--group-name",
        default=os.getenv(
            "SALAD_LTX25_GROUP_NAME",
            "ai-video-factory-ltx25-worker-v2",
        ),
    )
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--cleanup-stale-only", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/output/deployment-validation/ltx25-a2v"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generation_profile = (
        LTX_A2V_GENERATION_PROFILE
        if args.profile == "fast"
        else LTX_A2V_DEV_GENERATION_PROFILE
    )
    environment = _environment()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    avatar = args.avatar_image
    if avatar is None:
        avatar = args.output_dir / "default-avatar.png"
        _write_default_avatar(avatar)
        _event("A2V_FIXTURE_AVATAR_CREATED", path=avatar)
    avatar = avatar.resolve()
    audio = args.audio.resolve()
    if not avatar.is_file():
        raise SystemExit(f"Avatar image not found: {avatar}")
    if not audio.is_file():
        raise SystemExit(f"Speech audio not found: {audio}")

    input_audio_probe = _ffprobe(audio)
    input_audio_duration = _duration(input_audio_probe)
    input_audio_streams = [
        item for item in input_audio_probe.get("streams", []) if item.get("codec_type") == "audio"
    ]
    if not input_audio_streams:
        raise RuntimeError("smoke input does not contain an audio stream")
    input_audio_channels = int(input_audio_streams[0].get("channels") or 0)
    if input_audio_channels <= 0:
        raise RuntimeError("smoke input does not report a valid audio channel count")

    image_sha = sha256_file(avatar)
    audio_sha = sha256_file(audio)
    job_id = ltx_a2v_application_job_id(
        segment_id=args.segment_id,
        prompt=args.prompt,
        image_sha256=image_sha,
        audio_sha256=audio_sha,
        seed=args.seed,
        width=args.width,
        height=args.height,
        fps=args.fps,
        generation_profile=generation_profile,
    )
    queue_url = (
        "https://api.salad.com/api/public/organizations/"
        f"{environment['SALAD_ORGANIZATION']}/projects/{environment['SALAD_PROJECT']}"
        f"/queues/{args.queue_name}"
    )
    base_url = f"{queue_url}/jobs"
    if args.cleanup_stale_only:
        _cancel_stale_pending_application_jobs(
            base_url=base_url,
            api_key=environment["SALAD_API_KEY"],
            application_job_id=job_id,
        )
        _event(
            "A2V_STALE_PENDING_PREFLIGHT_DONE",
            application_job_id=job_id,
        )
        return

    image_suffix = avatar.suffix.lower() or ".png"
    audio_suffix = audio.suffix.lower() or ".wav"
    image_key = f"ltx25-a2v/smoke/images/{image_sha}{image_suffix}"
    audio_key = f"ltx25-a2v/smoke/audio/{audio_sha}{audio_suffix}"
    output_key = f"jobs/{job_id}/avatar_segment.mp4"
    metadata_key = f"jobs/{job_id}/metadata.json"

    image_type = mimetypes.guess_type(avatar.name)[0] or "application/octet-stream"
    audio_type = mimetypes.guess_type(audio.name)[0] or "application/octet-stream"
    request = InferenceJobRequest(
        job_id=job_id,
        task=LTX_A2V_TASK,
        inputs=[
            ObjectInput(
                name="image",
                key=image_key,
                sha256=image_sha,
                content_type=image_type,
            ),
            ObjectInput(
                name="audio",
                key=audio_key,
                sha256=audio_sha,
                content_type=audio_type,
            ),
        ],
        output=ObjectOutput(key=output_key, content_type="video/mp4"),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=metadata_key,
                content_type="application/json",
            )
        },
        max_attempts=1,
        parameters={
            "generation_profile": generation_profile,
            "prompt": args.prompt,
            "seed": args.seed,
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
        },
    )
    body = {
        "input": request.model_dump(mode="json", exclude_none=True),
        "metadata": {
            "application_job_id": job_id,
            "capability": "ltx25-a2v",
            "segment_id": args.segment_id,
        },
    }
    request_path = args.output_dir / f"job-request-{job_id}.json"
    response_path = args.output_dir / f"queue-response-{job_id}.json"
    request_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

    storage = R2ObjectStorage.create(
        endpoint_url=environment["R2_ENDPOINT_URL"],
        bucket=environment["R2_BUCKET"],
        access_key_id=environment["R2_ACCESS_KEY_ID"],
        secret_access_key=environment["R2_SECRET_ACCESS_KEY"],
    )
    _event("A2V_R2_UPLOAD_START", image_key=image_key, audio_key=audio_key)
    storage.upload(
        avatar,
        image_key,
        content_type=image_type,
        metadata={"purpose": "ltx25-a2v-avatar", "artifact-sha256": image_sha},
    )
    storage.upload(
        audio,
        audio_key,
        content_type=audio_type,
        metadata={"purpose": "ltx25-a2v-speech", "artifact-sha256": audio_sha},
    )
    _event("A2V_R2_UPLOAD_DONE")

    _cancel_stale_pending_application_jobs(
        base_url=base_url,
        api_key=environment["SALAD_API_KEY"],
        application_job_id=job_id,
    )
    _event("A2V_QUEUE_SUBMIT_START", queue=args.queue_name, application_job_id=job_id)
    created = _salad_request(
        base_url,
        environment["SALAD_API_KEY"],
        method="POST",
        body=body,
    )
    response_path.write_text(json.dumps(created, indent=2) + "\n", encoding="utf-8")
    _event("A2V_QUEUE_SUBMIT_DONE", salad_job_id=created["id"], status=created.get("status"))

    deadline = time.monotonic() + args.timeout_seconds
    pending_since = time.monotonic() if created.get("status") == "pending" else None
    pending_limit_seconds = args.pending_timeout_seconds
    pending_reallocations = 0
    current = created
    job_url = f"{base_url}/{created['id']}"
    try:
        while current["status"] not in {"succeeded", "failed", "cancelled"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"A2V job did not finish within {args.timeout_seconds}s")
            time.sleep(args.poll_seconds)
            current = _salad_request(job_url, environment["SALAD_API_KEY"])
            status = str(current["status"])
            _event("A2V_QUEUE_WAIT_STATUS", salad_job_id=created["id"], status=status)
            if status == "pending":
                pending_since = pending_since or time.monotonic()
                if time.monotonic() - pending_since >= pending_limit_seconds:
                    if pending_reallocations >= args.max_pending_reallocations:
                        raise TimeoutError(
                            "A2V transport remained pending after "
                            f"{pending_reallocations} Salad node reallocation(s)"
                        )
                    latest = _salad_request(
                        job_url,
                        environment["SALAD_API_KEY"],
                    )
                    latest_status = str(latest.get("status") or "")
                    if latest_status != "pending":
                        current = latest
                        pending_since = None
                        continue
                    pending_reallocations += 1
                    instance_id = _reallocate_single_group_instance(
                        organization=environment["SALAD_ORGANIZATION"],
                        project=environment["SALAD_PROJECT"],
                        group_name=args.group_name,
                        api_key=environment["SALAD_API_KEY"],
                    )
                    _event(
                        "A2V_PENDING_REALLOCATE",
                        salad_job_id=created["id"],
                        instance_id=instance_id,
                        reallocation=pending_reallocations,
                        max_reallocations=args.max_pending_reallocations,
                    )
                    pending_since = time.monotonic()
                    pending_limit_seconds = args.post_reallocation_pending_seconds
            else:
                pending_since = None
    except Exception:
        if str(current.get("status")) == "pending":
            _cancel_pending_transport(
                job_url,
                environment["SALAD_API_KEY"],
                str(created["id"]),
            )
        raise

    response_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    if current["status"] != "succeeded":
        raise RuntimeError(
            f"A2V Salad job ended {current['status']}: "
            + json.dumps(current, ensure_ascii=False, sort_keys=True)
        )

    queue_output: Any = current.get("output")
    if isinstance(queue_output, str):
        queue_output = json.loads(queue_output)
    if isinstance(queue_output, dict) and "detail" in queue_output:
        raise RuntimeError(
            "A2V worker terminal failure: "
            + str(queue_output["detail"])
        )
    if not isinstance(queue_output, dict) or queue_output.get("status") != "succeeded":
        raise RuntimeError(f"A2V worker returned invalid output: {queue_output!r}")
    artifact = queue_output.get("output") or {}
    if artifact.get("key") != output_key:
        raise RuntimeError(f"A2V worker returned unexpected output key: {artifact!r}")

    video_path = args.output_dir / "avatar_segment.mp4"
    metadata_path = args.output_dir / "metadata.json"
    _event("A2V_ARTIFACT_DOWNLOAD_START", output_key=output_key)
    storage.download(output_key, video_path)
    metadata_stored = storage.stat(metadata_key)
    if metadata_stored is None:
        raise RuntimeError("A2V metadata sidecar was not uploaded")
    storage.download(metadata_key, metadata_path)
    _event("A2V_ARTIFACT_DOWNLOAD_DONE", video=video_path, metadata=metadata_path)

    video_sha = sha256_file(video_path)
    if video_sha != artifact.get("sha256"):
        raise RuntimeError("downloaded A2V MP4 sha256 does not match worker response")
    metadata_sha = sha256_file(metadata_path)
    if metadata_sha != metadata_stored.metadata.get("artifact-sha256"):
        raise RuntimeError("downloaded A2V metadata sha256 does not match R2 metadata")

    probe = _ffprobe(video_path)
    probe_path = args.output_dir / "avatar_segment-ffprobe.json"
    probe_path.write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")
    video_streams = [item for item in probe.get("streams", []) if item.get("codec_type") == "video"]
    audio_streams = [item for item in probe.get("streams", []) if item.get("codec_type") == "audio"]
    if len(video_streams) != 1:
        raise RuntimeError(f"expected one video stream, got {len(video_streams)}")
    if len(audio_streams) != 1:
        raise RuntimeError(f"expected one audio stream, got {len(audio_streams)}")
    video_stream = video_streams[0]
    if (int(video_stream["width"]), int(video_stream["height"])) != (
        args.width,
        args.height,
    ):
        raise RuntimeError(
            f"A2V dimensions {video_stream['width']}x{video_stream['height']} "
            f"!= {args.width}x{args.height}"
        )
    numerator, denominator = str(video_stream.get("avg_frame_rate") or "0/1").split("/", 1)
    actual_fps = float(numerator) / float(denominator)
    if abs(actual_fps - args.fps) > 1e-6:
        raise RuntimeError(f"A2V fps {actual_fps} != requested {args.fps}")

    output_duration = _duration(probe)
    _validate_decodable_video(video_path)

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    required_metadata = {
        "input_audio_duration_seconds",
        "input_audio_channels",
        "conditioning_audio_channels",
        "audio_upmixed_to_stereo",
        "effective_audio_duration_seconds",
        "output_video_duration_seconds",
        "fps",
        "num_frames",
        "width",
        "height",
        "seed",
        "generation_mode",
        "generation_profile",
        "video_cfg_scale",
        "video_stg_scale",
        "video_modality_scale",
        "transformer_variant",
        "stage_1_steps",
        "stage_2_steps",
        "model_load_seconds",
        "inference_seconds",
        "video_encode_mux_seconds",
        "total_elapsed_seconds",
        "real_time_factor",
    }
    missing_metadata = required_metadata - set(metadata)
    if missing_metadata:
        raise RuntimeError(
            "A2V metadata is missing fields: " + ", ".join(sorted(missing_metadata))
        )
    if metadata["generation_mode"] != "audio_to_video":
        raise RuntimeError(f"unexpected A2V generation_mode: {metadata['generation_mode']!r}")
    if metadata["generation_profile"] != generation_profile:
        raise RuntimeError("A2V worker used a mismatched generation profile")
    expected_variant = "distilled" if args.profile == "fast" else "dev"
    expected_steps = 8 if args.profile == "fast" else 30
    if metadata["transformer_variant"] != expected_variant:
        raise RuntimeError("A2V worker used the wrong transformer variant")
    if int(metadata["stage_1_steps"]) != expected_steps or int(metadata["stage_2_steps"]) != 3:
        raise RuntimeError("A2V worker used an unexpected diffusion schedule")
    expected_cfg = 1.0 if args.profile == "fast" else 3.0
    if float(metadata["video_cfg_scale"]) != expected_cfg:
        raise RuntimeError("A2V worker used an unexpected CFG scale")
    if float(metadata["video_stg_scale"]) != 0.0 or float(metadata["video_modality_scale"]) != 1.0:
        raise RuntimeError("A2V worker did not disable extra STG/modality guidance")
    if int(metadata["input_audio_channels"]) != input_audio_channels:
        raise RuntimeError("A2V metadata input channel count does not match smoke input")
    if int(metadata["conditioning_audio_channels"]) != 2:
        raise RuntimeError("A2V conditioning audio must be stereo for the LTX audio VAE")
    expected_upmix = input_audio_channels == 1
    if bool(metadata["audio_upmixed_to_stereo"]) is not expected_upmix:
        raise RuntimeError(
            "A2V stereo-upmix metadata does not match the input audio channel count"
        )
    if (int(metadata["width"]), int(metadata["height"])) != (args.width, args.height):
        raise RuntimeError(
            "A2V metadata dimensions do not match the requested output geometry"
        )
    if int(metadata["fps"]) != args.fps:
        raise RuntimeError("A2V metadata fps does not match the requested fps")
    if int(metadata["seed"]) != args.seed:
        raise RuntimeError("A2V metadata seed does not match the requested seed")

    metadata_output_duration = float(metadata["output_video_duration_seconds"])
    effective_audio_duration = float(metadata["effective_audio_duration_seconds"])
    duration_probe_tolerance = max(0.05, 1.5 / args.fps)
    if abs(metadata_output_duration - output_duration) > duration_probe_tolerance:
        raise RuntimeError(
            "A2V metadata/output duration mismatch: "
            f"{metadata_output_duration:.6f}s vs {output_duration:.6f}s"
        )
    if abs(effective_audio_duration - output_duration) > duration_probe_tolerance:
        raise RuntimeError(
            "A2V conditioned audio/video duration mismatch: "
            f"{effective_audio_duration:.6f}s vs {output_duration:.6f}s"
        )
    max_grid_snap_seconds = 8.0 / args.fps
    if effective_audio_duration > input_audio_duration + duration_probe_tolerance:
        raise RuntimeError("A2V output audio unexpectedly exceeds the input speech duration")
    if input_audio_duration - effective_audio_duration > (
        max_grid_snap_seconds + duration_probe_tolerance
    ):
        raise RuntimeError(
            "A2V output duration lost more than one temporal-grid interval: "
            f"input={input_audio_duration:.6f}s effective={effective_audio_duration:.6f}s"
        )

    print(f"application_job_id={job_id}")
    print(f"salad_job_id={created['id']}")
    print(f"input_audio_duration_seconds={input_audio_duration:.6f}")
    print(f"input_audio_channels={input_audio_channels}")
    print(f"conditioning_audio_channels={metadata['conditioning_audio_channels']}")
    print(f"audio_upmixed_to_stereo={metadata['audio_upmixed_to_stereo']}")
    print(f"output_video_duration_seconds={output_duration:.6f}")
    print(f"resolution={args.width}x{args.height}")
    print(f"fps={actual_fps:.6f}")
    print(f"num_frames={metadata['num_frames']}")
    print(f"generation_profile={metadata['generation_profile']}")
    print(f"transformer_variant={metadata['transformer_variant']}")
    print(f"stage_1_steps={metadata['stage_1_steps']}")
    print(f"stage_2_steps={metadata['stage_2_steps']}")
    print(f"video_cfg_scale={metadata['video_cfg_scale']}")
    print(f"video_stg_scale={metadata['video_stg_scale']}")
    print(f"video_modality_scale={metadata['video_modality_scale']}")
    print(f"inference_seconds={metadata['inference_seconds']}")
    print(f"video_encode_mux_seconds={metadata['video_encode_mux_seconds']}")
    print(f"total_elapsed_seconds={metadata['total_elapsed_seconds']}")
    print(f"real_time_factor={metadata['real_time_factor']}")
    print(f"peak_vram_bytes={metadata.get('peak_vram_bytes')}")
    print(f"video_sha256={video_sha}")
    print(f"metadata_sha256={metadata_sha}")
    print(f"video={video_path.resolve()}")
    print(f"metadata={metadata_path.resolve()}")
    print(f"ffprobe={probe_path.resolve()}")
    if args.max_generation_seconds > 0:
        actual_seconds = float(metadata["total_elapsed_seconds"])
        _event(
            "A2V_BENCHMARK",
            total_seconds=actual_seconds,
            limit_seconds=args.max_generation_seconds,
        )
        if actual_seconds > args.max_generation_seconds:
            raise RuntimeError(
                "A2V generation exceeded the requested performance budget: "
                f"{actual_seconds:.2f}s > {args.max_generation_seconds:.2f}s"
            )
    _event("A2V_SMOKE_DONE", status="succeeded", salad_job_id=created["id"])


if __name__ == "__main__":
    main()
