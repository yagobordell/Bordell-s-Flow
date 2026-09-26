from __future__ import annotations

import argparse
import json
import math
import mimetypes
import shutil
import subprocess
import sys
from array import array
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ai_video_factory.config import settings
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.gpu_failures import DEFAULT_GPU_MAX_ATTEMPTS
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.providers.r2 import create_r2_storage
from ai_video_factory.workers.ltx25 import (
    LTX_A2V_DEFAULT_PROMPT,
    LTX_A2V_DEV_GENERATION_PROFILE,
    LTX_A2V_GENERATION_PROFILE,
    LTX_A2V_GUIDED_GENERATION_PROFILE,
    LTX_A2V_REFERENCE_COMPILED_GENERATION_PROFILE,
    LTX_A2V_REFERENCE_GENERATION_PROFILE,
    LTX_A2V_TASK,
    ltx_a2v_application_job_id,
)


def _required(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is required for the real LTX A2V smoke.")
    return value.strip()


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
        raise RuntimeError(
            "generated video failed decode validation: " + completed.stderr[-1200:]
        )


def _voice_rms_profile(
    path: Path,
    *,
    sample_rate: int = 16000,
    window_seconds: float = 0.02,
) -> tuple[list[float], float]:
    """Decode mono PCM and return an RMS envelope on an absolute time grid."""

    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("ffmpeg is required for A2V voice-tail validation")
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    samples = array("h")
    samples.frombytes(completed.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise RuntimeError(f"A2V voice-tail validation decoded no audio: {path}")

    window_samples = max(1, round(sample_rate * window_seconds))
    rms_values: list[float] = []
    for start in range(0, len(samples), window_samples):
        chunk = samples[start : start + window_samples]
        if not chunk:
            continue
        mean_square = sum(float(value) * float(value) for value in chunk) / len(chunk)
        rms_values.append(math.sqrt(mean_square))
    if not rms_values:
        raise RuntimeError(f"A2V voice-tail validation produced no RMS windows: {path}")
    return rms_values, window_samples / float(sample_rate)


def _active_windows(rms_values: list[float]) -> list[int]:
    peak = max(rms_values, default=0.0)
    threshold = max(300.0, peak * 0.08)
    return [index for index, value in enumerate(rms_values) if value >= threshold]


def _validate_voice_tail_profiles(
    input_rms: list[float],
    output_rms: list[float],
    *,
    window_seconds: float,
) -> None:
    """Require the final voiced windows to survive AAC muxing at the same times."""

    input_active = _active_windows(input_rms)
    output_active = _active_windows(output_rms)
    if not input_active or not output_active:
        raise RuntimeError(
            "A2V smoke audio does not contain measurable voiced activity"
        )

    position_tolerance_seconds = 0.06
    position_tolerance_windows = math.ceil(
        position_tolerance_seconds / window_seconds
    )
    input_last = input_active[-1]
    output_last = output_active[-1]
    if output_last + position_tolerance_windows < input_last:
        raise RuntimeError(
            "reference A2V output lost the final voiced position: "
            f"input_last={input_last * window_seconds:.3f}s "
            f"output_last={output_last * window_seconds:.3f}s "
            f"tolerance={position_tolerance_seconds:.3f}s"
        )

    input_peak = max(input_rms)
    output_peak = max(output_rms)
    tail_candidates = [
        index
        for index in input_active
        if index >= input_last - math.ceil(0.30 / window_seconds)
    ][-5:]
    matched = 0
    for index in tail_candidates:
        if index >= len(output_rms):
            continue
        input_normalized = input_rms[index] / input_peak
        output_normalized = output_rms[index] / output_peak
        # The output is AAC, so compare presence rather than sample equality.
        if input_normalized >= 0.08 and output_normalized >= 0.03:
            matched += 1

    required_matches = max(1, math.ceil(len(tail_candidates) * 0.8))
    if matched < required_matches:
        raise RuntimeError(
            "reference A2V output lost energy from the final voiced tail: "
            f"matched_windows={matched}/{len(tail_candidates)} "
            f"required={required_matches}"
        )


def _validate_reference_voice_tail(input_audio: Path, output_video: Path) -> None:
    input_rms, input_window = _voice_rms_profile(input_audio)
    output_rms, output_window = _voice_rms_profile(output_video)
    if abs(input_window - output_window) > 1e-9:
        raise RuntimeError("A2V voice-tail profiles use mismatched time grids")
    _validate_voice_tail_profiles(
        input_rms,
        output_rms,
        window_seconds=input_window,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit and verify one real LTX-2.5 image+speech A2V job through Postgres/R2."
    )
    parser.add_argument("--avatar-image", type=Path)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--prompt", default=LTX_A2V_DEFAULT_PROMPT)
    parser.add_argument("--segment-id", default="smoke-001")
    parser.add_argument(
        "--profile",
        choices=("fast", "reference", "reference-compiled", "dev", "guided"),
        default="reference",
        help="Functional avatar A2V baseline; fast/dev remain explicit comparison modes.",
    )
    parser.add_argument("--max-generation-seconds", type=float, default=0.0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--seed", type=int, default=4242)
    parser.add_argument("--timeout-seconds", type=float, default=10800.0)
    parser.add_argument("--pending-timeout-seconds", type=float, default=None)
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/output/deployment-validation/ltx25-a2v"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.profile == "reference-compiled":
        raise SystemExit(
            "reference-compiled failed real visual and cold-latency validation on "
            "2026-09-26. Use --profile reference; no paid job will be submitted."
        )
    profiles = {
        "fast": LTX_A2V_GENERATION_PROFILE,
        "reference": LTX_A2V_REFERENCE_GENERATION_PROFILE,
        "reference-compiled": LTX_A2V_REFERENCE_COMPILED_GENERATION_PROFILE,
        "dev": LTX_A2V_DEV_GENERATION_PROFILE,
        "guided": LTX_A2V_GUIDED_GENERATION_PROFILE,
    }
    profile = profiles[args.profile]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    avatar = args.avatar_image
    if avatar is None:
        avatar = args.output_dir / "default-avatar.png"
        _write_default_avatar(avatar)
    avatar = avatar.resolve()
    audio = args.audio.resolve()
    if not avatar.is_file():
        raise SystemExit(f"Avatar image not found: {avatar}")
    if not audio.is_file():
        raise SystemExit(f"Speech audio not found: {audio}")

    input_probe = _ffprobe(audio)
    input_duration = _duration(input_probe)
    audio_streams = [
        item for item in input_probe.get("streams", []) if item.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise RuntimeError("smoke input does not contain an audio stream")
    input_channels = int(audio_streams[0].get("channels") or 0)
    if input_channels <= 0:
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
        generation_profile=profile,
    )

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
            ObjectInput(name="image", key=image_key, sha256=image_sha, content_type=image_type),
            ObjectInput(name="audio", key=audio_key, sha256=audio_sha, content_type=audio_type),
        ],
        output=ObjectOutput(key=output_key, content_type="video/mp4"),
        sidecar_outputs={
            "metadata": ObjectOutput(key=metadata_key, content_type="application/json")
        },
        max_attempts=DEFAULT_GPU_MAX_ATTEMPTS,
        parameters={
            "generation_profile": profile,
            "prompt": args.prompt,
            "seed": args.seed,
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
        },
    )
    (args.output_dir / f"job-request-{job_id}.json").write_text(
        request.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    storage = create_r2_storage(
        endpoint_url=_required("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required(
            "R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key
        ),
    )
    executor = InferenceJobExecutor(
        queue=PostgresJobQueueClient(
            dsn=_required("POSTGRES_DSN", settings.postgres_dsn),
        ),
        storage=storage,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        pending_timeout_seconds=(
            args.pending_timeout_seconds
            if args.pending_timeout_seconds is not None
            else (10800.0 if args.profile == "guided" else 1800.0)
        ),
    )
    executor.ensure_input(
        avatar,
        key=image_key,
        sha256=image_sha,
        content_type=image_type,
        metadata={"purpose": "ltx25-a2v-avatar"},
    )
    executor.ensure_input(
        audio,
        key=audio_key,
        sha256=audio_sha,
        content_type=audio_type,
        metadata={"purpose": "ltx25-a2v-speech"},
    )

    response = executor.execute(
        request,
        metadata={
            "capability": "ltx25-a2v",
            "segment_id": args.segment_id,
        },
    )
    video_path = args.output_dir / "avatar_segment.mp4"
    metadata_path = args.output_dir / "metadata.json"
    executor.download_output(response, video_path)

    metadata_stored = storage.stat(metadata_key)
    if metadata_stored is None:
        raise RuntimeError("A2V metadata sidecar was not uploaded")
    storage.download(metadata_key, metadata_path)

    video_sha = sha256_file(video_path)
    if video_sha != response.output.sha256:
        raise RuntimeError("downloaded A2V MP4 sha256 does not match worker response")
    metadata_sha = sha256_file(metadata_path)
    if metadata_sha != metadata_stored.metadata.get("artifact-sha256"):
        raise RuntimeError("downloaded A2V metadata sha256 does not match R2 metadata")

    probe = _ffprobe(video_path)
    (args.output_dir / "avatar_segment-ffprobe.json").write_text(
        json.dumps(probe, indent=2) + "\n",
        encoding="utf-8",
    )
    video_streams = [
        item for item in probe.get("streams", []) if item.get("codec_type") == "video"
    ]
    audio_outputs = [
        item for item in probe.get("streams", []) if item.get("codec_type") == "audio"
    ]
    if len(video_streams) != 1 or len(audio_outputs) != 1:
        raise RuntimeError("A2V output must contain exactly one video and one audio stream")

    video_stream = video_streams[0]
    if (int(video_stream["width"]), int(video_stream["height"])) != (
        args.width,
        args.height,
    ):
        raise RuntimeError("A2V output dimensions do not match the requested geometry")
    numerator, denominator = str(video_stream.get("avg_frame_rate") or "0/1").split("/", 1)
    actual_fps = float(numerator) / float(denominator)
    if abs(actual_fps - args.fps) > 1e-6:
        raise RuntimeError(f"A2V fps {actual_fps} != requested {args.fps}")

    output_duration = _duration(probe)
    _validate_decodable_video(video_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    required = {
        "input_audio_duration_seconds",
        "input_audio_channels",
        "conditioning_audio_channels",
        "audio_upmixed_to_stereo",
        "effective_audio_duration_seconds",
        "output_video_duration_seconds",
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
    missing = required - set(metadata)
    if missing:
        raise RuntimeError("A2V metadata is missing fields: " + ", ".join(sorted(missing)))
    if metadata["generation_mode"] != "audio_to_video":
        raise RuntimeError("A2V worker returned the wrong generation mode")
    if metadata["generation_profile"] != profile:
        raise RuntimeError("A2V worker used a mismatched generation profile")

    distilled_profile = args.profile in {"fast", "reference", "reference-compiled"}
    expected_variant = "distilled" if distilled_profile else "dev"
    expected_steps = 8 if distilled_profile else 30
    expected_cfg = 1.0 if distilled_profile else 3.0
    if metadata["transformer_variant"] != expected_variant:
        raise RuntimeError("A2V worker used the wrong transformer variant")
    if int(metadata["stage_1_steps"]) != expected_steps or int(metadata["stage_2_steps"]) != 3:
        raise RuntimeError("A2V worker used an unexpected diffusion schedule")
    if float(metadata["video_cfg_scale"]) != expected_cfg:
        raise RuntimeError("A2V worker used an unexpected CFG scale")
    if args.profile == "guided":
        if (
            float(metadata["video_stg_scale"]) != 1.0
            or float(metadata["video_modality_scale"]) != 3.0
            or float(metadata.get("video_rescale_scale", -1)) != 0.7
            or metadata.get("video_stg_blocks") != [29]
        ):
            raise RuntimeError("guided A2V worker did not preserve pinned upstream guidance")
    elif (
        float(metadata["video_stg_scale"]) != 0.0
        or float(metadata["video_modality_scale"]) != 1.0
    ):
        raise RuntimeError("legacy A2V worker did not disable extra STG/modality guidance")
    expected_compilation = "blocks" if args.profile == "reference-compiled" else "eager"
    if metadata.get("transformer_compilation", "eager") != expected_compilation:
        raise RuntimeError("A2V worker used an unexpected transformer compilation mode")
    if int(metadata["input_audio_channels"]) != input_channels:
        raise RuntimeError("A2V metadata input channel count does not match smoke input")
    if int(metadata["conditioning_audio_channels"]) != 2:
        raise RuntimeError("A2V conditioning audio must be stereo")
    if bool(metadata["audio_upmixed_to_stereo"]) is not (input_channels == 1):
        raise RuntimeError("A2V stereo-upmix metadata is inconsistent")

    effective_audio_duration = float(metadata["effective_audio_duration_seconds"])
    metadata_output_duration = float(metadata["output_video_duration_seconds"])
    tolerance = max(0.05, 1.5 / args.fps)
    if abs(metadata_output_duration - output_duration) > tolerance:
        raise RuntimeError("A2V metadata/output duration mismatch")
    if abs(effective_audio_duration - output_duration) > tolerance:
        raise RuntimeError("A2V conditioned audio/video duration mismatch")
    if args.profile in {"reference", "reference-compiled", "guided"}:
        reference_required = {
            "generation_recipe",
            "stage_1_sampler",
            "stage_2_sampler",
            "stage_1_image_strength",
            "stage_2_image_strength",
            "audio_frozen_stage_1",
            "audio_frozen_stage_2",
            "decoded_speech_samples",
            "conditioning_audio_samples",
            "audio_padding_samples",
            "audio_padding_seconds",
            "grid_video_duration_seconds",
            "ltx_model_revision",
            "quantization",
            "offload_mode",
        }
        reference_missing = reference_required - set(metadata)
        if reference_missing:
            raise RuntimeError(
                "reference A2V metadata is missing fields: "
                + ", ".join(sorted(reference_missing))
            )
        expected_recipe = (
            "distilled_reference"
            if args.profile in {"reference", "reference-compiled"}
            else "upstream_guided_dev"
        )
        expected_sampler = (
            "euler_ancestral"
            if args.profile in {"reference", "reference-compiled"}
            else "euler"
        )
        if metadata["generation_recipe"] != expected_recipe:
            raise RuntimeError("A2V worker used the wrong generation recipe")
        if metadata["stage_1_sampler"] != expected_sampler:
            raise RuntimeError("A2V worker used the wrong Stage 1 sampler")
        if metadata["stage_2_sampler"] != "euler":
            raise RuntimeError("reference A2V Stage 2 is not Euler")
        expected_strength = (
            0.7 if args.profile in {"reference", "reference-compiled"} else 1.0
        )
        if float(metadata["stage_1_image_strength"]) != expected_strength:
            raise RuntimeError("A2V worker used the wrong Stage 1 image strength")
        if float(metadata["stage_2_image_strength"]) != 1.0:
            raise RuntimeError("reference A2V Stage 2 image strength is not 1.0")
        if not bool(metadata["audio_frozen_stage_1"]) or not bool(
            metadata["audio_frozen_stage_2"]
        ):
            raise RuntimeError("reference A2V did not freeze audio in both stages")

        decoded_samples = int(metadata["decoded_speech_samples"])
        conditioning_samples = int(metadata["conditioning_audio_samples"])
        padding_samples = int(metadata["audio_padding_samples"])
        if decoded_samples <= 0:
            raise RuntimeError("reference A2V did not report decoded speech samples")
        if conditioning_samples < decoded_samples:
            raise RuntimeError("reference A2V conditioning truncated decoded speech samples")
        if padding_samples != conditioning_samples - decoded_samples:
            raise RuntimeError("reference A2V padding metadata is inconsistent")
        if padding_samples < 0:
            raise RuntimeError("reference A2V reported negative audio padding")

        grid_duration = float(metadata["grid_video_duration_seconds"])
        if grid_duration + tolerance < input_duration:
            raise RuntimeError("reference A2V grid does not cover the full input speech")
        if effective_audio_duration + tolerance < input_duration:
            raise RuntimeError("reference A2V output audio is shorter than the input speech")
        if abs(effective_audio_duration - grid_duration) > tolerance:
            raise RuntimeError("reference A2V output audio does not cover its snapped grid")
        _validate_reference_voice_tail(audio, video_path)
    else:
        if effective_audio_duration > input_duration + tolerance:
            raise RuntimeError("A2V output audio unexpectedly exceeds input speech duration")
        if input_duration - effective_audio_duration > (8.0 / args.fps) + tolerance:
            raise RuntimeError("A2V output lost more than one temporal-grid interval")

    if args.max_generation_seconds > 0:
        actual_seconds = float(metadata["total_elapsed_seconds"])
        if actual_seconds > args.max_generation_seconds:
            raise RuntimeError(
                "A2V generation exceeded the requested performance budget: "
                f"{actual_seconds:.2f}s > {args.max_generation_seconds:.2f}s"
            )

    print(f"application_job_id={job_id}")
    print(f"postgres_job_id={response.job_id}")
    print(f"replayed={str(response.replayed).lower()}")
    print(f"input_audio_duration_seconds={input_duration:.6f}")
    print(f"output_video_duration_seconds={output_duration:.6f}")
    print(f"generation_profile={metadata['generation_profile']}")
    print(f"inference_seconds={metadata['inference_seconds']}")
    print(f"total_elapsed_seconds={metadata['total_elapsed_seconds']}")
    print(f"video_sha256={video_sha}")
    print(f"metadata_sha256={metadata_sha}")
    print(f"video={video_path.resolve()}")
    print(f"metadata={metadata_path.resolve()}")


if __name__ == "__main__":
    main()
