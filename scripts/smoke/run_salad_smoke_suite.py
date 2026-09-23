from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any

from PIL import Image

from ai_video_factory.config import settings
from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.inference.storage import R2ObjectStorage, sha256_file
from ai_video_factory.providers import (
    SaladBreezeSpeechProvider,
    SaladQwenImage21Provider,
    SaladWhisperTranscriptionProvider,
)
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.salad_queue import SaladJobQueueClient
from ai_video_factory.workers.breeze_tts2 import BREEZE_TTS2_MODEL_ID
from ai_video_factory.workers.qwen_image_21 import (
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_MODEL_ID,
    QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    QWEN_IMAGE_21_PRODUCTION_SIZE,
    QWEN_IMAGE_21_PRODUCTION_WIDTH,
)
from ai_video_factory.workers.whisper import WHISPER_MODEL_ID

_SERVICE_ORDER = ("breeze_tts2", "whisper", "qwen_image_21", "ltx25")
_DEFAULT_OUTPUT_DIR = Path("data/output/deployment-validation")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one real Salad worker smoke test or the complete Breeze -> Whisper -> "
            "Qwen-Image-2.1 -> LTX validation chain."
        )
    )
    parser.add_argument(
        "--service",
        choices=(*_SERVICE_ORDER, "all"),
        default="all",
        help="Worker to validate. 'all' executes the complete dependency chain.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_DEFAULT_OUTPUT_DIR,
        help="Persistent directory for validation artifacts and reports.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=7200.0,
        help="Maximum wait for each remote inference job.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=settings.inference_client_poll_seconds,
        help="Queue polling interval used by provider-backed smoke tests.",
    )
    parser.add_argument(
        "--whisper-audio",
        type=Path,
        default=None,
        help="Optional WAV input when validating Whisper independently.",
    )
    parser.add_argument(
        "--ltx-keyframe",
        type=Path,
        default=None,
        help="Optional PNG input when validating LTX independently.",
    )
    return parser.parse_args()


def _required(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to .env before real deployment validation.")
    return value.strip()


def _stack_identity() -> tuple[str, str]:
    manifest = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    stack = manifest["stack"]
    organization = settings.salad_organization or str(stack["organization"])
    project = settings.salad_project or str(stack["project"])
    return organization, project


def _storage() -> R2ObjectStorage:
    return R2ObjectStorage.create(
        endpoint_url=_required("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
    )


def _executor(
    queue_name: str,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> InferenceJobExecutor:
    organization, project = _stack_identity()
    queue = SaladJobQueueClient(
        organization=organization,
        project=project,
        queue_name=queue_name,
        api_key=_required("SALAD_API_KEY", settings.salad_api_key),
    )
    return InferenceJobExecutor(
        queue=queue,
        storage=_storage(),
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )


def _write_report(output_dir: Path, service: str, payload: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{service}-smoke.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _update_summary(output_dir: Path) -> None:
    entries: dict[str, Any] = {}
    for service in _SERVICE_ORDER:
        path = output_dir / f"{service}-smoke.json"
        if path.is_file():
            entries[service] = json.loads(path.read_text(encoding="utf-8"))
    (output_dir / "smoke-summary.json").write_text(
        json.dumps({"schema_version": "1", "services": entries}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


async def _smoke_breeze(args: argparse.Namespace) -> None:
    started = time.monotonic()
    executor = _executor(
        settings.salad_breeze_tts2_queue_name,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    provider = SaladBreezeSpeechProvider(
        executor=executor,
        temp_dir=settings.temp_dir / "deployment-validation-breeze",
        cfg_scale=settings.breeze_tts_cfg_scale,
        seed=4242,
    )
    text = "The AI video factory deployment smoke test is running successfully."
    speech = await provider.generate_speech(
        text=text,
        model=BREEZE_TTS2_MODEL_ID,
        voice=settings.breeze_tts_voice,
        instructions="Clear neutral English documentary narration, calm and concise.",
        speed=1.0,
        output_format="wav",
    )
    destination = args.output_dir / "breeze-smoke.wav"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(speech.content)
    with wave.open(str(destination), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        channels = handle.getnchannels()
        duration = frames / rate if rate else 0.0
    if duration <= 0:
        raise RuntimeError("Breeze smoke WAV has no measurable duration")
    report = {
        "status": "succeeded",
        "service": "breeze_tts2",
        "model": BREEZE_TTS2_MODEL_ID,
        "wall_seconds": round(time.monotonic() - started, 3),
        "artifact": destination.as_posix(),
        "size_bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "duration_seconds": round(duration, 3),
        "sample_rate": rate,
        "channels": channels,
        "text": text,
    }
    path = _write_report(args.output_dir, "breeze_tts2", report)
    print(f"Breeze TTS 2 smoke: OK -> {destination}")
    print(path)


async def _smoke_whisper(args: argparse.Namespace) -> None:
    started = time.monotonic()
    audio_path = args.whisper_audio or (args.output_dir / "breeze-smoke.wav")
    if not audio_path.is_file():
        raise SystemExit(
            "Whisper smoke input is missing. Run --service breeze_tts2 first "
            "or pass --whisper-audio."
        )
    executor = _executor(
        settings.salad_whisper_queue_name,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    provider = SaladWhisperTranscriptionProvider(
        executor=executor,
        temp_dir=settings.temp_dir / "deployment-validation-whisper",
    )
    words = await provider.transcribe_words(
        audio_path.read_bytes(),
        filename=audio_path.name,
        model=WHISPER_MODEL_ID,
        prompt="AI video factory deployment smoke test.",
        language="en",
    )
    if not words:
        raise RuntimeError("Whisper smoke returned no words")
    report = {
        "status": "succeeded",
        "service": "whisper",
        "model": WHISPER_MODEL_ID,
        "wall_seconds": round(time.monotonic() - started, 3),
        "input": audio_path.as_posix(),
        "word_count": len(words),
        "words": [
            {
                "text": word.text,
                "start_seconds": word.start_seconds,
                "end_seconds": word.end_seconds,
            }
            for word in words
        ],
    }
    path = _write_report(args.output_dir, "whisper", report)
    print(f"Whisper smoke: OK -> {len(words)} words")
    print(path)


async def _smoke_qwen_image_21(args: argparse.Namespace) -> None:
    started = time.monotonic()
    executor = _executor(
        settings.salad_qwen_image_21_queue_name,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    provider = SaladQwenImage21Provider(
        executor=executor,
        temp_dir=settings.temp_dir / "deployment-validation-qwen-image-21",
        task_name=QWEN_IMAGE_21_KEYFRAME_TASK,
    )
    smoke_id = f"smoke-{time.time_ns()}"
    prompt = (
        "A cinematic 16:9 documentary still of a compact robotic cinema camera on a "
        "clean studio table, realistic materials, soft directional studio lighting, "
        "stable composition, no text, deployment validation image. "
        f"Internal validation id {smoke_id}; do not render the identifier."
    )
    image = await provider.generate_image(
        prompt=prompt,
        model=QWEN_IMAGE_21_MODEL_ID,
        size=QWEN_IMAGE_21_PRODUCTION_SIZE,
        quality="high",
        output_format="png",
    )
    if image.metadata.get("replayed") != "false":
        raise RuntimeError(
            "Qwen-Image-2.1 smoke must execute fresh inference; cached replay is not accepted"
        )
    destination = args.output_dir / "qwen-image-21-keyframe.png"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(image.content)
    if destination.stat().st_size <= 0:
        raise RuntimeError("Qwen-Image-2.1 smoke returned an empty PNG")
    with Image.open(destination) as opened:
        opened.load()
        if opened.size != (
            QWEN_IMAGE_21_PRODUCTION_WIDTH,
            QWEN_IMAGE_21_PRODUCTION_HEIGHT,
        ):
            raise RuntimeError(
                "Qwen-Image-2.1 smoke returned unexpected dimensions: "
                f"{opened.width}x{opened.height}"
            )
        if opened.format != "PNG":
            raise RuntimeError(
                f"Qwen-Image-2.1 smoke returned unexpected format: {opened.format}"
            )
    report = {
        "status": "succeeded",
        "service": "qwen_image_21",
        "model": QWEN_IMAGE_21_MODEL_ID,
        "task": QWEN_IMAGE_21_KEYFRAME_TASK,
        "wall_seconds": round(time.monotonic() - started, 3),
        "smoke_id": smoke_id,
        "artifact": destination.as_posix(),
        "size": QWEN_IMAGE_21_PRODUCTION_SIZE,
        "size_bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "provider_metadata": image.metadata,
    }
    path = _write_report(args.output_dir, "qwen_image_21", report)
    print(f"Qwen-Image-2.1 smoke: OK -> {destination}")
    print(path)


def _ltx_environment() -> dict[str, str]:
    organization, project = _stack_identity()
    values = {
        "SALAD_API_KEY": _required("SALAD_API_KEY", settings.salad_api_key),
        "SALAD_ORGANIZATION": organization,
        "SALAD_PROJECT": project,
        "R2_ENDPOINT_URL": _required("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        "R2_BUCKET": _required("R2_BUCKET", settings.r2_bucket),
        "R2_ACCESS_KEY_ID": _required("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        "R2_SECRET_ACCESS_KEY": _required(
            "R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key
        ),
        "SALAD_LTX25_QUEUE_NAME": os.getenv(
            "SALAD_LTX25_QUEUE_NAME", "ai-video-factory-ltx25-jobs-v2"
        ),
    }
    return {**os.environ, **values}


def _write_model_list(path: Path, values: list[Any]) -> None:
    path.write_text(
        json.dumps([value.model_dump(mode="json") for value in values], indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_ltx_landscape_keyframe(source: Path, destination: Path) -> Path:
    """Create a deterministic 16:9 conditioning image for the LTX landscape smoke."""

    with Image.open(source) as opened:
        image = opened.convert("RGB")
    target_ratio = 16 / 9
    source_ratio = image.width / image.height
    if source_ratio > target_ratio:
        crop_width = round(image.height * target_ratio)
        left = (image.width - crop_width) // 2
        image = image.crop((left, 0, left + crop_width, image.height))
    elif source_ratio < target_ratio:
        crop_height = round(image.width / target_ratio)
        top = (image.height - crop_height) // 2
        image = image.crop((0, top, image.width, top + crop_height))

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.resize((1280, 720), Image.Resampling.LANCZOS).save(
        destination,
        format="PNG",
    )
    return destination


def _smoke_ltx25(args: argparse.Namespace) -> None:
    started = time.monotonic()
    keyframe_path = args.ltx_keyframe or (args.output_dir / "qwen-image-21-keyframe.png")
    if not keyframe_path.is_file():
        raise SystemExit(
            "LTX smoke keyframe is missing. Run --service qwen_image_21 first "
            "or pass --ltx-keyframe."
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    inputs_dir = args.output_dir / "ltx-inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    local_keyframe = _prepare_ltx_landscape_keyframe(
        keyframe_path,
        inputs_dir / "ltx-keyframe-16x9.png",
    )

    keyframes_path = inputs_dir / "storyboard_keyframes.json"
    prompts_path = inputs_dir / "video_prompts.json"
    timings_path = inputs_dir / "shot_timings.json"
    _write_model_list(
        keyframes_path,
        [StoryboardKeyframe(shot_id=1, uri=local_keyframe.name)],
    )
    _write_model_list(
        prompts_path,
        [
            VideoPrompt(
                shot_id=1,
                prompt=(
                    "A slow cinematic push-in toward the robotic camera, subtle parallax, "
                    "stable framing, realistic studio light, no cuts."
                ),
            )
        ],
    )
    _write_model_list(
        timings_path,
        [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=1.0)],
    )

    cloud_dir = args.output_dir / "ltx25-cloud"
    command = [
        sys.executable,
        "scripts/smoke/submit_ltx25_smoke.py",
        "--shot-id",
        "1",
        "--keyframes",
        str(keyframes_path),
        "--prompts",
        str(prompts_path),
        "--timings",
        str(timings_path),
        "--width",
        "1280",
        "--height",
        "720",
        "--fps",
        "24",
        "--timeout-seconds",
        str(int(args.timeout_seconds)),
        "--poll-seconds",
        str(max(1, int(args.poll_seconds))),
        "--output-dir",
        str(cloud_dir),
    ]
    log_path = args.output_dir / "ltx25-smoke.log"
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_ltx_environment(),
    )
    if process.stdout is None:  # pragma: no cover - PIPE guarantees stdout
        raise RuntimeError("LTX smoke process did not expose stdout")
    with log_path.open("w", encoding="utf-8") as log_handle:
        for line in process.stdout:
            print(line, end="", flush=True)
            log_handle.write(line)
            log_handle.flush()
    returncode = process.wait()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, command)

    videos = sorted(cloud_dir.glob("shot_*.mp4"))
    if len(videos) != 1 or videos[0].stat().st_size <= 0:
        raise RuntimeError("LTX smoke did not produce exactly one non-empty MP4")
    video = videos[0]
    report = {
        "status": "succeeded",
        "service": "ltx25",
        "wall_seconds": round(time.monotonic() - started, 3),
        "input_keyframe": keyframe_path.as_posix(),
        "artifact": video.as_posix(),
        "size_bytes": video.stat().st_size,
        "sha256": sha256_file(video),
        "log": log_path.as_posix(),
    }
    path = _write_report(args.output_dir, "ltx25", report)
    print(f"LTX 2.5 smoke: OK -> {video}")
    print(path)


async def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    selected = _SERVICE_ORDER if args.service == "all" else (args.service,)

    try:
        for service in selected:
            if service == "breeze_tts2":
                await _smoke_breeze(args)
            elif service == "whisper":
                await _smoke_whisper(args)
            elif service == "qwen_image_21":
                await _smoke_qwen_image_21(args)
            elif service == "ltx25":
                _smoke_ltx25(args)
            else:  # pragma: no cover - argparse prevents this branch
                raise AssertionError(service)
            _update_summary(args.output_dir)
    except Exception:
        _update_summary(args.output_dir)
        raise

    print(f"Deployment validation artifacts: {args.output_dir.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
