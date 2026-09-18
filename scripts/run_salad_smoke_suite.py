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

from ai_video_factory.config import settings
from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.inference.storage import R2ObjectStorage, sha256_file
from ai_video_factory.providers import (
    SaladBreezeSpeechProvider,
    SaladFlux2KleinImageProvider,
    SaladIdeogramImageProvider,
    SaladWhisperTranscriptionProvider,
)
from ai_video_factory.providers.ideogram_caption import (
    IdeogramCaptionPlan,
    IdeogramElementPlan,
    IdeogramStylePlan,
    render_ideogram_caption,
)
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.salad_queue import SaladJobQueueClient
from ai_video_factory.workers.breeze_tts2 import BREEZE_TTS2_MODEL_ID
from ai_video_factory.workers.flux2_klein import (
    FLUX2_KLEIN_MODEL_ID,
    FLUX2_KLEIN_REFERENCE_TASK,
)
from ai_video_factory.workers.ideogram4 import (
    IDEOGRAM4_KEYFRAME_TASK,
    IDEOGRAM4_MODEL_ID,
    IDEOGRAM4_REFERENCE_TASK,
)
from ai_video_factory.workers.whisper import WHISPER_MODEL_ID

_SERVICE_ORDER = ("breeze_tts2", "whisper", "ideogram4", "ltx25")
_REPORT_SERVICES = (*_SERVICE_ORDER, "flux2_klein")
_DEFAULT_OUTPUT_DIR = Path("data/output/deployment-validation")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one real Salad worker smoke test or the complete Breeze -> Whisper -> "
            "Ideogram -> LTX validation chain."
        )
    )
    parser.add_argument(
        "--service",
        choices=(*_SERVICE_ORDER, "flux2_klein", "all"),
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
    for service in _REPORT_SERVICES:
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


def _smoke_caption() -> str:
    return render_ideogram_caption(
        IdeogramCaptionPlan(
            high_level_description=(
                "A cinematic vertical documentary still of a compact robotic camera on a clean "
                "studio table, used as a deterministic deployment validation image."
            ),
            style=IdeogramStylePlan(
                aesthetics="clean cinematic documentary photography, realistic materials",
                lighting="soft directional studio light with subtle practical highlights",
                medium="digital cinema photography",
                render_mode="photo",
                render_description="photorealistic high-detail product documentary frame",
                color_palette=["#1A1A1A", "#D8D8D8", "#5B7C99"],
            ),
            background="minimal dark neutral studio with gentle depth falloff",
            elements=[
                IdeogramElementPlan(
                    description="small robotic cinema camera centered on the table",
                    bbox=[250, 180, 800, 820],
                    color_palette=["#1A1A1A", "#D8D8D8"],
                )
            ],
        )
    )


async def _smoke_ideogram(args: argparse.Namespace) -> None:
    started = time.monotonic()
    executor = _executor(
        settings.salad_ideogram4_queue_name,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    caption = _smoke_caption()
    artifacts: dict[str, dict[str, Any]] = {}
    for purpose, task, size in (
        ("reference", IDEOGRAM4_REFERENCE_TASK, "1024x1024"),
        ("keyframe", IDEOGRAM4_KEYFRAME_TASK, "1024x1536"),
    ):
        provider = SaladIdeogramImageProvider(
            executor=executor,
            temp_dir=settings.temp_dir / f"deployment-validation-ideogram-{purpose}",
            task_name=task,
        )
        image = await provider.generate_image(
            prompt=caption,
            model=IDEOGRAM4_MODEL_ID,
            size=size,
            quality="high",
            output_format="png",
        )
        destination = args.output_dir / f"ideogram-{purpose}.png"
        args.output_dir.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(image.content)
        if destination.stat().st_size <= 0:
            raise RuntimeError(f"Ideogram {purpose} smoke returned an empty PNG")
        artifacts[purpose] = {
            "task": task,
            "size": size,
            "path": destination.as_posix(),
            "size_bytes": destination.stat().st_size,
            "sha256": sha256_file(destination),
        }
    report = {
        "status": "succeeded",
        "service": "ideogram4",
        "model": IDEOGRAM4_MODEL_ID,
        "quality": "V4_QUALITY_48",
        "wall_seconds": round(time.monotonic() - started, 3),
        "artifacts": artifacts,
    }
    path = _write_report(args.output_dir, "ideogram4", report)
    print("Ideogram 4 reference + keyframe smoke: OK")
    print(path)


async def _smoke_flux2_klein(args: argparse.Namespace) -> None:
    started = time.monotonic()
    executor = _executor(
        settings.salad_flux2_klein_queue_name,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    provider = SaladFlux2KleinImageProvider(
        executor=executor,
        temp_dir=settings.temp_dir / "deployment-validation-flux2-klein",
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
    )
    prompt = (
        "A cinematic documentary photograph of a compact robotic camera on a dark studio "
        "table, soft directional light, realistic materials, centered composition."
    )
    image = await provider.generate_image(
        prompt=prompt,
        model=FLUX2_KLEIN_MODEL_ID,
        size="1024x1024",
        quality="high",
        output_format="png",
    )
    destination = args.output_dir / "flux2-klein-smoke.png"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(image.content)
    if destination.stat().st_size <= 8 or not image.content.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
        raise RuntimeError("FLUX.2 Klein smoke returned an invalid PNG")
    report = {
        "status": "succeeded",
        "service": "flux2_klein",
        "model": FLUX2_KLEIN_MODEL_ID,
        "wall_seconds": round(time.monotonic() - started, 3),
        "artifact": destination.as_posix(),
        "size_bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "metadata": image.metadata,
        "width": 1024,
        "height": 1024,
    }
    path = _write_report(args.output_dir, "flux2_klein", report)
    print(f"FLUX.2 Klein smoke: OK -> {destination}")
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
            "SALAD_LTX25_QUEUE_NAME", "ai-video-factory-ltx25-jobs"
        ),
    }
    return {**os.environ, **values}


def _write_model_list(path: Path, values: list[Any]) -> None:
    path.write_text(
        json.dumps([value.model_dump(mode="json") for value in values], indent=2) + "\n",
        encoding="utf-8",
    )


def _smoke_ltx25(args: argparse.Namespace) -> None:
    started = time.monotonic()
    keyframe_path = args.ltx_keyframe or (args.output_dir / "ideogram-keyframe.png")
    if not keyframe_path.is_file():
        raise SystemExit(
            "LTX smoke keyframe is missing. Run --service ideogram4 first or pass --ltx-keyframe."
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    inputs_dir = args.output_dir / "ltx-inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    local_keyframe = inputs_dir / "ideogram-keyframe.png"
    if keyframe_path.resolve() != local_keyframe.resolve():
        local_keyframe.write_bytes(keyframe_path.read_bytes())

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
        "scripts/submit_ltx25_smoke.py",
        "--shot-id",
        "1",
        "--keyframes",
        str(keyframes_path),
        "--prompts",
        str(prompts_path),
        "--timings",
        str(timings_path),
        "--width",
        "768",
        "--height",
        "1280",
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
            elif service == "ideogram4":
                await _smoke_ideogram(args)
            elif service == "flux2_klein":
                await _smoke_flux2_klein(args)
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
