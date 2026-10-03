"""Standalone B1.1 → B1.2 → B2 runner with a selectable local script library."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import uuid4

from dotenv import load_dotenv
from PIL import Image

from ai_video_factory.bots import run_b_pipeline
from ai_video_factory.bots.billing import ApiCostLedger
from ai_video_factory.bots.contracts import (
    B2Input,
    B2InputBlock,
    B2Output,
    B11Output,
    B12Output,
    ProfessionalB2Output,
)
from ai_video_factory.bots.fish_audio_workflow import (
    AudioRegenerationLimitReached,
    SttRetryLimitReached,
    generate_fish_audio,
    regenerate_audio_block,
    retranscribe_audio_block,
    unfinished_audio_runs,
)
from ai_video_factory.bots.professional_voice import generate_professional_audio
from ai_video_factory.bots.workflow import (
    materialize_blocks,
    validate_beats,
    validate_visuals,
)
from ai_video_factory.compositor.static_preview import (
    StaticPreviewError,
    SttAlignmentError,
    _verified_asset,
    build_stt_beat_timeline,
    generate_static_preview,
)
from ai_video_factory.config import settings
from ai_video_factory.inference.capacity_controller import (
    read_capacity_controller_health,
    validate_capacity_controller_dsn,
)
from ai_video_factory.inference.salad import (
    DEFAULT_SALAD_API_BASE_URL,
    SaladClient,
    SaladDispatchConfig,
)
from ai_video_factory.inference.salad_billing import SaladCostMeter, read_salad_costs
from ai_video_factory.inference.salad_ltx_worker_prepare import ensure_a2v_image
from ai_video_factory.managed_layout import publish_managed_run, sync_managed_outputs
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.providers.ai33_speech import AI33SpeechClient
from ai_video_factory.providers.avatar_library import (
    avatar_directory,
    ensure_avatar_meta,
    organize_avatar,
)
from ai_video_factory.providers.avatar_qr import ensure_avatar_qr
from ai_video_factory.providers.avatar_style import ensure_avatar_style
from ai_video_factory.providers.avatar_voice import ensure_avatar_voice, valid_voice_provider
from ai_video_factory.providers.ebook_covers import ensure_ebook_covers
from ai_video_factory.providers.grid_crops import crop_grid_assets
from ai_video_factory.providers.image_grids import BLACK_PANEL_SOURCE, GridPlanError
from ai_video_factory.providers.openai_batch_images import (
    ImageGenerationError,
    ImageOptions,
    clear_b2_images_for_rerun,
    generate_b2_images,
)
from ai_video_factory.providers.openai_stt import OpenAISttClient
from ai_video_factory.providers.optium_image_upscaler import create_optium_image_upscaler
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.providers.professional_avatar_voice import ensure_professional_avatar_voice
from ai_video_factory.providers.professional_image_finishing import finish_professional_images
from ai_video_factory.providers.r2 import create_r2_storage
from ai_video_factory.publishing import publish_completed_run
from ai_video_factory.workflows.b_video import (
    generate_b2_ltx_video,
    has_pending_b2_video,
)
from ai_video_factory.workflows.casero_cta import prepare_casero_cta

DEFAULT_SCRIPTS_DIR = Path("data/input")
DEFAULT_AVATARS_DIR = Path("data/avatar")
_STAGES = ("B1.1", "B1.2", "B2")
_METRIC_STAGES = ("B1.1", "CTA", "CTA Ebook", "CTA Motion", "B1.2", "B2", "V1", "V2", "V3")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "script_file",
        nargs="?",
        type=Path,
        help="Optional direct path to a UTF-8 script (for existing commands/automation).",
    )
    parser.add_argument(
        "--scripts-dir",
        type=Path,
        default=DEFAULT_SCRIPTS_DIR,
        help="Directory containing selectable .txt scripts (default: data/input).",
    )
    parser.add_argument(
        "--script",
        metavar="NAME",
        help="Select a .txt file by name from --scripts-dir, with or without its .txt suffix.",
    )
    parser.add_argument(
        "--list-scripts",
        action="store_true",
        help="List available scripts without starting inference or requiring an API key.",
    )
    parser.add_argument(
        "--avatars-dir",
        type=Path,
        default=DEFAULT_AVATARS_DIR,
        help="Directory containing selectable PNG avatars (default: data/avatar).",
    )
    parser.add_argument(
        "--avatar",
        metavar="NAME",
        help="Select a PNG avatar from --avatars-dir, with or without its .png suffix.",
    )
    parser.add_argument(
        "--ebook-cover", action="append", metavar="NOMBRE",
        help="Modo casero: portada PNG en data/ sin extension; repetir para portada secundaria.",
    )
    parser.add_argument(
        "--qr-url",
        metavar="URL",
        help="Enlace del ebook para crear el QR de un avatar nuevo sin preguntar en la CLI.",
    )
    parser.add_argument(
        "--tts-provider", choices=("fishaudio", "minimax", "elevenlabs"),
        help=(
            "Solo Casero: 1=Fish Audio, 2=MiniMax, 3=ElevenLabs. "
            "Si es interactivo, pregunta antes del ID."
        ),
    )
    voices = parser.add_mutually_exclusive_group()
    voices.add_argument(
        "--voice-id",
        metavar="NAME_OR_ID",
        help="ID del proveedor Casero o nombre/ID exacto de voz Gemini Profesional.",
    )
    voices.add_argument(
        "--voice-audio",
        metavar="FILENAME",
        help="Nombre del audio de clonación situado junto al PNG del avatar.",
    )
    voices.add_argument(
        "--default-voice",
        action="store_true",
        help="Guardar la voz predeterminada para el avatar sin preguntar.",
    )
    parser.add_argument(
        "--replace-professional-voice",
        action="store_true",
        help=(
            "Solo un nuevo run Profesional con --voice-id: actualiza la voz de este avatar "
            "para futuras ejecuciones. Los runs anteriores conservan su voz guardada."
        ),
    )
    parser.add_argument(
        "--qr-only",
        action="store_true",
        help="Genera o reutiliza el QR del avatar localmente, sin guion ni API.",
    )
    parser.add_argument(
        "--list-avatars",
        action="store_true",
        help="List available PNG avatars without starting inference or requiring an API key.",
    )
    parser.add_argument(
        "--publish-only",
        action="store_true",
        help="Publish an existing finished LTX run, without API or GPU requests.",
    )
    parser.add_argument(
        "--youtube-only",
        action="store_true",
        help="Distribute an already finished VERIFIED local run to YouTube, no GPU jobs.",
    )
    parser.add_argument(
        "--managed-target",
        type=Path,
        default=None,
        help="Optional exact data/<avatar>/<script> folder managed by run_managed.py.",
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=Path("data/results"),
        help="Independent immutable results library (default: data/results).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output root; each script gets a child folder (default: data/output).",
    )
    parser.add_argument("--max-parallel-calls", type=int, default=8)
    parser.add_argument(
        "--style", "--b2-style", dest="visual_style",
        choices=("casero", "profesional"),
        help="B2 visual style; interactive runs ask when omitted.",
    )
    parser.add_argument(
        "--from",
        dest="from_stage",
        choices=("B1.1", "B1.2", "B2"),
        default="B1.1",
        help="Start at this stage using validated earlier outputs in data/output/<script>.",
    )
    parser.add_argument(
        "--stop",
        dest="stop_stage",
        choices=_STAGES,
        default=None,
        help="Stop after this planning stage, without starting TTS or image generation.",
    )
    parser.add_argument(
        "--skip-images",
        "--no-image",
        dest="skip_images",
        action="store_true",
        help="Save B2 outputs without official OpenAI image generation.",
    )
    parser.add_argument(
        "--images-only",
        action="store_true",
        help=(
            "Generate or resume standard official OpenAI images from a saved B2 plan, "
            "without repeating the planning bots."
        ),
    )
    parser.add_argument(
        "--generate-video",
        "--videos-only",
        dest="videos_only",
        action="store_true",
        help="Generate or resume LTX video from a saved completed preview; no B bots or TTS.",
    )
    parser.add_argument(
        "--no-video",
        action="store_true",
        help="Skip paid LTX I2V/A2V; keep the local static preview.",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Run B1.1/B1.2/B2 without the style-specific voice directors or TTS.",
    )
    parser.add_argument(
        "--regenerate-audio",
        action="store_true",
        help="Create fresh audio using the saved B result and its style-specific TTS.",
    )
    parser.add_argument(
        "--resume-audio",
        action="store_true",
        help="Resume saved Casero or Gemini block TTS safely without rerunning B bots.",
    )
    args = parser.parse_args()
    if args.replace_professional_voice and (
        args.voice_id is None or args.voice_audio is not None or args.default_voice
        or args.from_stage != "B1.1" or args.regenerate_audio or args.resume_audio
        or args.videos_only or args.images_only or args.qr_only or args.publish_only
        or args.youtube_only or args.list_avatars or args.list_scripts
    ):
        parser.error(
            "--replace-professional-voice requires --voice-id on a new planning run"
        )
    if args.youtube_only and not settings.youtube_enabled:
        parser.error("--youtube-only requires YOUTUBE_ENABLED=true")
    if args.youtube_only and (
        args.publish_only or args.qr_only or args.list_scripts or args.list_avatars
        or args.avatar is not None or args.qr_url is not None
        or args.voice_id is not None or args.voice_audio is not None or args.default_voice
        or args.visual_style is not None or args.from_stage != "B1.1"
        or args.stop_stage is not None or args.images_only or args.videos_only
        or args.skip_images or args.no_video or args.no_audio
        or args.regenerate_audio or args.resume_audio
    ):
        parser.error("--youtube-only cannot be combined with generation options")
    if args.publish_only and (
        args.qr_only or args.list_scripts or args.list_avatars
        or args.avatar is not None or args.qr_url is not None
        or args.voice_id is not None or args.voice_audio is not None or args.default_voice
        or args.visual_style is not None or args.from_stage != "B1.1"
        or args.stop_stage is not None or args.images_only or args.videos_only
        or args.skip_images or args.no_video or args.no_audio
        or args.regenerate_audio or args.resume_audio
    ):
        parser.error("--publish-only accepts only a script, --output and --results-root.")
    if args.qr_only and (
        args.script_file is not None
        or args.script is not None
        or args.from_stage != "B1.1"
        or args.stop_stage is not None
        or args.images_only
        or args.videos_only
        or args.skip_images
        or args.no_video
        or args.no_audio
        or args.regenerate_audio
        or args.resume_audio
        or args.output is not None
        or args.list_scripts
        or args.list_avatars
        or args.voice_id is not None or args.voice_audio is not None or args.default_voice
        or args.visual_style is not None
    ):
        parser.error("--qr-only only accepts --avatar, --avatars-dir and --qr-url.")
    if args.visual_style is not None and (
        args.images_only or args.videos_only or args.regenerate_audio
        or args.resume_audio or args.list_scripts or args.list_avatars
    ):
        parser.error("--style is for planning runs; resumed assets use the saved B2 style.")
    if args.regenerate_audio and args.resume_audio:
        parser.error("Choose either --regenerate-audio or --resume-audio.")
    if (args.regenerate_audio or args.resume_audio) and (
        args.images_only
        or args.videos_only
        or args.no_audio
        or args.skip_images
        or args.from_stage != "B1.1"
        or args.stop_stage is not None
        or args.avatar is not None
        or args.qr_url is not None
        or args.voice_id is not None or args.voice_audio is not None or args.default_voice
    ):
        parser.error(
            "Audio-only modes cannot be combined with images, --from, "
            "--no-audio or --avatar."
        )
    if args.videos_only and (
        args.images_only or args.no_audio or args.no_video or args.skip_images
        or args.regenerate_audio or args.resume_audio
        or args.from_stage != "B1.1" or args.stop_stage is not None
        or args.avatar is not None or args.qr_url is not None
        or args.voice_id is not None or args.voice_audio is not None or args.default_voice
    ):
        parser.error(
            "--generate-video cannot be combined with other generation flags."
        )
    if args.images_only and args.no_audio:
        parser.error("--images-only does not run audio; omit --no-audio.")
    if args.images_only and (
        args.skip_images or args.from_stage != "B1.1" or args.stop_stage is not None
    ):
        parser.error("--images-only cannot be combined with --no-image, --from or --stop.")
    if args.images_only and (
        args.avatar is not None or args.qr_url is not None
        or args.voice_id is not None or args.voice_audio is not None or args.default_voice
    ):
        parser.error("--images-only uses the saved avatar; omit --avatar and --qr-url.")
    if args.stop_stage is not None and (
        _STAGES.index(args.stop_stage) < _STAGES.index(args.from_stage)
    ):
        parser.error("--stop must be the same as or later than --from.")
    if args.script_file is not None and args.script is not None:
        parser.error("Specify either a direct script path or --script, not both.")
    if args.list_scripts and args.list_avatars:
        parser.error("Choose either --list-scripts or --list-avatars.")
    if (args.list_scripts or args.list_avatars) and (
        args.qr_only
        or args.script_file is not None
        or args.script is not None
        or args.avatar is not None
        or args.qr_url is not None
        or args.voice_id is not None or args.voice_audio is not None or args.default_voice
        or args.from_stage != "B1.1"
        or args.stop_stage is not None
        or args.images_only
        or args.videos_only
        or args.skip_images
        or args.no_video
        or args.no_audio
        or args.regenerate_audio
        or args.resume_audio
    ):
        parser.error("Listing cannot be combined with generation options.")
    return args


def available_scripts(scripts_dir: Path) -> list[Path]:
    """Discover only ordinary .txt files from the selected local library."""
    if not scripts_dir.is_dir():
        return []
    return sorted(
        (
            path
            for path in scripts_dir.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".txt"
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )


def select_script(
    *,
    scripts_dir: Path,
    script_name: str | None = None,
    script_file: Path | None = None,
    interactive: bool | None = None,
) -> Path:
    """Select one file without changing its content or moving it."""
    if script_name is not None and script_file is not None:
        raise SystemExit("Specify either a script path or --script, not both.")
    if script_file is not None:
        if not script_file.is_file():
            raise SystemExit(f"Script file not found: {script_file}")
        return script_file

    scripts = available_scripts(scripts_dir)
    if not scripts:
        raise SystemExit(
            f"No .txt scripts found in {scripts_dir}. "
            "Please add one or more UTF-8 .txt scripts there."
        )
    if script_name is not None:
        requested = (
            script_name if script_name.lower().endswith(".txt") else f"{script_name}.txt"
        )
        match = next((path for path in scripts if path.name == requested), None)
        if match is None:
            available = ", ".join(path.name for path in scripts)
            raise SystemExit(
                f"Script {requested!r} not found in {scripts_dir}. Available: {available}"
            )
        return match

    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise SystemExit(
            "Script selection requires an interactive terminal. "
            "Use --script NAME.txt or --list-scripts."
        )

    print(f"Available scripts in {scripts_dir}:")
    for number, path in enumerate(scripts, start=1):
        print(f"  {number}. {path.name}")

    while True:
        try:
            selection = input("Select a script number (q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise SystemExit("Script selection cancelled.") from exc
        if selection.lower() in {"q", "quit"}:
            raise SystemExit("Script selection cancelled.")
        if selection.isdecimal() and 1 <= int(selection) <= len(scripts):
            return scripts[int(selection) - 1]
        print(f"Enter a number between 1 and {len(scripts)}, or q to cancel.")


def select_visual_style(selected: str | None, *, interactive: bool | None = None) -> str:
    """Pick a style for a new planning run, without prompting unattended scripts."""
    if selected is not None:
        return selected
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        return "casero"
    print("Estilo visual de B2:\n  1. Casero\n  2. Profesional")
    while True:
        try:
            answer = input("Elige estilo (1/2; q para cancelar): ").strip().lower()
        except (EOFError, KeyboardInterrupt) as exc:
            raise SystemExit("Selección de estilo cancelada.") from exc
        if answer in {"1", "casero"}:
            return "casero"
        if answer in {"2", "profesional"}:
            return "profesional"
        if answer in {"q", "quit"}:
            raise SystemExit("Selección de estilo cancelada.")
        print("Introduce 1 o 2, o q para cancelar.")


def _recover_avatar_layout(
    avatar_file: Path, avatars_dir: Path, *, reference_audio: str | None = None
) -> Path:
    """Finish an interrupted migration before QR/voice selection can prompt again."""
    folder = avatar_directory(avatar_file, avatars_dir)
    if (
        avatar_file.parent.resolve() != avatars_dir.resolve()
        or folder.is_symlink()
        or folder.is_junction()
        or (folder.exists() and (not folder.is_dir() or any(folder.iterdir())))
    ):
        return organize_avatar(
            avatar_file, avatars_dir, reference_audio=reference_audio
        )
    return avatar_file


def available_avatars(avatars_dir: Path) -> list[Path]:
    """Discover unmigrated PNGs and canonical one-level avatar folders."""
    if not avatars_dir.is_dir():
        return []
    avatars: list[Path] = []
    for entry in avatars_dir.iterdir():
        if entry.is_symlink() or entry.is_junction():
            continue
        if entry.is_file() and entry.suffix.lower() == ".png":
            avatars.append(entry)
        elif entry.is_dir() and not entry.name.startswith("."):
            avatars.extend(
                image for image in entry.iterdir()
                if (
                    image.is_file()
                    and not image.is_symlink()
                    and image.suffix.lower() == ".png"
                    and image.stem == entry.name
                )
            )
    avatars.sort(key=lambda path: (path.name.casefold(), path.name))
    names: set[str] = set()
    for avatar in avatars:
        key = avatar.name.casefold()
        if key in names:
            raise SystemExit(
                f"Hay dos avatares con el mismo nombre: {avatar.name}. "
                "Resuelve el duplicado antes de continuar."
            )
        names.add(key)
    return avatars


def select_avatar(
    *,
    avatars_dir: Path,
    avatar_name: str | None = None,
    interactive: bool | None = None,
) -> Path:
    """Require an explicit avatar: by exact filename or an interactive menu."""
    avatars = available_avatars(avatars_dir)
    if not avatars:
        raise SystemExit(
            f"No PNG avatars found in {avatars_dir}. "
            "Add one or more valid .png images there before running the pipeline."
        )
    if avatar_name is not None:
        requested = (
            avatar_name if avatar_name.lower().endswith(".png") else f"{avatar_name}.png"
        )
        match = next((path for path in avatars if path.name == requested), None)
        if match is None:
            available = ", ".join(path.name for path in avatars)
            raise SystemExit(
                f"Avatar {requested!r} not found in {avatars_dir}. Available: {available}"
            )
        return match

    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise SystemExit(
            "Avatar selection requires an interactive terminal. "
            "Use --avatar NAME.png or --list-avatars."
        )

    print(f"Available avatars in {avatars_dir}:")
    for number, path in enumerate(avatars, start=1):
        print(f"  {number}. {path.name}")

    while True:
        try:
            selection = input("Select an avatar number (q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise SystemExit("Avatar selection cancelled.") from exc
        if selection.lower() in {"q", "quit"}:
            raise SystemExit("Avatar selection cancelled.")
        if selection.isdecimal() and 1 <= int(selection) <= len(avatars):
            return avatars[int(selection) - 1]
        print(f"Enter a number between 1 and {len(avatars)}, or q to cancel.")


def load_avatar_png(avatar_file: Path) -> tuple[bytes, int, int]:
    """Validate the actual PNG, not just its extension, and keep exact source bytes."""
    try:
        data = avatar_file.read_bytes()
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG":
                raise ValueError("The selected image is not a PNG")
            width, height = image.size
            image.verify()
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Invalid PNG avatar: {avatar_file}") from exc
    return data, width, height


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _publish_if_ready(
    output: Path, script_file: Path, results_root: Path,
    managed_target: Path | None = None,
    *,
    fail_on_youtube_error: bool = False,
) -> Path | None:
    """Publish only a finished LTX export, never a deferred run or static preview."""
    report_path = output / "run_report.json"
    if report_path.is_symlink() or not report_path.is_file():
        return None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    run = report.get("run", {})
    video = run.get("video_generation", {}) if isinstance(run, dict) else {}
    if not isinstance(video, dict) or video.get("status") != "completed":
        return None
    result = (
        publish_managed_run(output, script_file, managed_target)
        if managed_target is not None else
        publish_completed_run(output, script_file, results_root)
    )
    print(f"  Published result: {result}", flush=True)
    # Independent post-publication distribution. Never let YouTube failure
    # invalidate a verified local result or re-run costly generation stages.
    if settings.youtube_enabled:
        try:
            from ai_video_factory.integrations.youtube.hook import publish_verified_result

            outcome = publish_verified_result(
                output=output,
                published=result,
                script_name=script_file.stem,
                config=settings,
                managed_target=managed_target,
            )
            print(f"  YouTube publication: {outcome['status']}", flush=True)
            if outcome.get("url"):
                print(f"  YouTube URL: {outcome['url']}", flush=True)
        except Exception as exc:
            from ai_video_factory.integrations.youtube.errors import YouTubeError
            from ai_video_factory.integrations.youtube.hook import record_distribution_error

            try:
                record_distribution_error(output, exc)
            except Exception:
                print("  YouTube error report could not be saved.", flush=True)
            # YouTubeError strings are known-safe; arbitrary provider errors
            # can contain credentials and must never be printed.
            detail = (
                type(exc).__name__ + ": " + str(exc)
                if isinstance(exc, YouTubeError) else "unexpected distribution failure"
            )
            print(
                "  YouTube publication failed separately from video generation: " + detail,
                flush=True,
            )
            print(
                "  Verified MP4 is retained. Resolve publication using "
                "scripts/youtube/publish_run.py without regenerating.",
                flush=True,
            )
            if fail_on_youtube_error:
                raise RuntimeError(
                    "YouTube distribution failed; the verified local MP4 is unchanged"
                ) from None
    return result


def _prepare_output(
    output_root: Path, script_file: Path, *, avatar_file: Path | None = None
) -> Path:
    """Replace only an identifiable B-pipeline output folder for this script."""
    root = output_root.resolve()
    source = script_file.resolve()
    avatar_source = avatar_file.resolve() if avatar_file is not None else None
    avatar_library = (
        avatar_source.parent.parent
        if avatar_source is not None and avatar_source.parent.name == avatar_source.stem
        else avatar_source.parent if avatar_source is not None else None
    )
    cwd = Path.cwd().resolve()
    if (
        root == cwd
        or root in cwd.parents
        or source.is_relative_to(root)
        or (
            avatar_source is not None
            and (
                avatar_source.is_relative_to(root)
                or (avatar_library is not None and root.is_relative_to(avatar_library))
            )
        )
    ):
        raise SystemExit(
            "--output must be a dedicated output directory, not the repository, "
            "a parent of it, or an input directory (script or avatar)."
        )
    destination = output_root / script_file.stem
    if destination.is_symlink() or destination.is_junction():
        raise SystemExit(f"Refusing to delete a linked output directory: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise SystemExit(f"Output path is not a directory: {destination}")
        recognized = (
            destination / "run_report.json",
            destination / ".b_pipeline_run.json",
            destination / "B1.1" / "input.json",
            destination / "b1_1.json",  # Previous standalone runner layout.
            destination / "visual_plan.json",
        )
        if any(destination.iterdir()) and not any(path.is_file() for path in recognized):
            raise SystemExit(
                f"Refusing to delete unrelated files in {destination}; "
                "choose a dedicated --output root."
            )
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=False)
    return destination


def _prepare_resume_output(
    output_root: Path, script_file: Path, *, avatar_file: Path
) -> Path:
    root = output_root.resolve()
    source = script_file.resolve()
    avatar_source = avatar_file.resolve()
    avatar_library = (
        avatar_source.parent.parent
        if avatar_source.parent.name == avatar_source.stem
        else avatar_source.parent
    )
    cwd = Path.cwd().resolve()
    if (
        root == cwd
        or root in cwd.parents
        or source.is_relative_to(root)
        or avatar_source.is_relative_to(root)
        or root.is_relative_to(avatar_library)
    ):
        raise SystemExit(
            "--output must be a dedicated output directory, not an input or repository path."
        )
    destination = output_root / script_file.stem
    if destination.is_symlink() or destination.is_junction():
        raise SystemExit(f"Refusing to resume from a linked output directory: {destination}")
    if not destination.is_dir():
        raise SystemExit(
            f"No prior run directory exists for --from {script_file.stem}: {destination}"
        )
    report_path = destination / "run_report.json"
    if not report_path.is_file() or report_path.is_symlink():
        raise SystemExit(f"Cannot resume: missing valid run report at {report_path}")
    return destination


def _load_resume_checkpoint(
    output: Path,
    *,
    start_from: str,
    script: str,
    script_sha256: str,
) -> tuple[
    dict[str, object], B11Output, tuple[B12Output, ...] | None,
    dict[int, B12Output], dict[int, B2Output | ProfessionalB2Output],
]:
    try:
        report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not isinstance(report.get("run"), dict):
            raise ValueError("the saved run report is malformed")
        run = report["run"]
        if run.get("script_sha256") != script_sha256:
            raise ValueError("the selected script differs from the saved run")
        b11_dir = output / "B1.1"
        b11_path = b11_dir / "output.json"
        if b11_dir.is_symlink() or b11_path.is_symlink() or not b11_path.is_file():
            raise ValueError("the saved B1.1 output is missing or linked")
        b11_data = json.loads(b11_path.read_text(encoding="utf-8"))
        b11 = B11Output.model_validate(b11_data)
        if b11.error is not None or b11.blocks is None or b11.narrative_core is None:
            raise ValueError("the saved B1.1 response is not a successful checkpoint")
        materialized = materialize_blocks(script, b11.blocks)

        partial_b12: dict[int, B12Output] = {}
        partial_b2: dict[int, B2Output | ProfessionalB2Output] = {}
        if (start_from == "B1.2" and run.get("status") == "failed"
            and not (output / "B1.2" / "merged_output.json").exists()):
            for block in materialized:
                path = output / "B1.2" / f"block_{block.block_id}" / "output.json"
                if path.is_symlink() or path.parent.is_symlink():
                    raise ValueError("unsafe saved B1.2 block")
                if path.is_file():
                    saved = B12Output.model_validate(json.loads(path.read_text(encoding="utf-8")))
                    validate_beats(saved, block)
                    partial_b12[block.block_id] = saved

        b12_results: tuple[B12Output, ...] | None = None
        if start_from == "B2":
            b12_dir = output / "B1.2"
            b12_path = b12_dir / "merged_output.json"
            if b12_dir.is_symlink() or b12_path.is_symlink() or not b12_path.is_file():
                raise ValueError("the saved B1.2 merge is missing or linked")
            merged = json.loads(b12_path.read_text(encoding="utf-8"))
            if not isinstance(merged, dict):
                raise ValueError("the saved B1.2 merge is malformed")
            if (
                merged.get("pipeline_stage") != "B1.2"
                or merged.get("narrative_core") != b11.narrative_core.model_dump()
                or not isinstance(merged.get("blocks"), list)
                or len(merged["blocks"]) != len(materialized)
            ):
                raise ValueError("the saved B1.2 merge does not match the B1.1 checkpoint")
            parsed: list[B12Output] = []
            for expected_id, item, block in zip(
                range(1, len(materialized) + 1), merged["blocks"], materialized, strict=True
            ):
                response = B12Output.model_validate(
                    {
                        "pipeline_stage": "B1.2",
                        "block_id": item["block_id"],
                        "beats": item["beats"],
                        "error": None,
                    }
                )
                if response.block_id != expected_id:
                    raise ValueError("the saved B1.2 block IDs are not consecutive")
                validate_beats(response, block)
                parsed.append(response)
            b12_results = tuple(parsed)
            if (run.get("status") == "failed"
                and not (output / "B2" / "merged_output.json").exists()):
                expected_type = (
                    ProfessionalB2Output if run.get("visual_style") == "profesional"
                    else B2Output
                )
                for block in materialized:
                    path = output / "B2" / f"block_{block.block_id}" / "output.json"
                    if path.is_symlink() or path.parent.is_symlink():
                        raise ValueError("unsafe saved B2 block")
                    if path.is_file():
                        saved = expected_type.model_validate(
                            json.loads(path.read_text(encoding="utf-8"))
                        )
                        meta = b11.blocks[block.block_id - 1]
                        supplied = B2Input(
                            narrative_core=b11.narrative_core,
                            current_block_id=block.block_id,
                            blocks=(B2InputBlock(
                                block_id=meta.block_id, type=meta.type,
                                emotional_entry=meta.emotional_entry,
                                emotional_exit=meta.emotional_exit, span=meta.span,
                                beats=b12_results[block.block_id - 1].beats,
                            ),),
                        )
                        validate_visuals(saved, supplied)
                        partial_b2[block.block_id] = saved

        costs = report["api_costs"]
        timings = report["timings"]
        if (
            not isinstance(costs, dict)
            or not isinstance(costs.get("requests"), list)
            or not isinstance(timings, dict)
        ):
            raise ValueError("the saved run report lacks request or timing history")
        return report, b11, b12_results, partial_b12, partial_b2
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"Cannot resume from {start_from}: {exc}") from exc


def _clear_outputs_from(output: Path, start_from: str) -> None:
    first_stage = _STAGES.index(start_from)
    for stage in _STAGES[first_stage:]:
        target = output / stage
        if target.is_symlink() or target.is_junction():
            raise SystemExit(f"Refusing to clear linked stage output: {target}")
        if target.exists():
            if not target.is_dir():
                raise SystemExit(f"Stage output is not a directory: {target}")
            shutil.rmtree(target)
    visual_plan = output / "visual_plan.json"
    if visual_plan.is_symlink():
        raise SystemExit(f"Refusing to remove linked visual plan: {visual_plan}")
    if visual_plan.exists():
        if not visual_plan.is_file():
            raise SystemExit(f"Visual plan path is not a file: {visual_plan}")
        visual_plan.unlink()



def _image_options() -> ImageOptions:
    return ImageOptions(
        model_id=settings.openai_image_model,
        size=settings.openai_image_size,
        quality=settings.openai_image_quality,
    )


async def _generate_images(output: Path, plan: dict[str, object]) -> dict[str, object]:
    professional = plan.get("visual_style") == "profesional"
    # Preflight required image-upscaler configuration before *paid* GPT image
    # submissions. A missing service must never produce a partial-cost run.
    needs_x2 = professional and any(
        beat.get("prompt") is not None
        and beat.get("visual_type") in {"media_image", "avatar_media"}
        for block in plan["blocks"] for beat in block["beats"]
    )
    upscaler = None
    if needs_x2:
        required = {
            "IMAGE_UPSCALER_URL": settings.image_upscaler_url,
            "SALAD_API_KEY": settings.salad_api_key,
            "R2_ENDPOINT_URL": settings.r2_endpoint_url,
            "R2_BUCKET": settings.r2_bucket,
            "R2_ACCESS_KEY_ID": settings.r2_access_key_id,
            "R2_SECRET_ACCESS_KEY": settings.r2_secret_access_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ImageGenerationError(
                "Professional image x2 requires configured OptiumLabs Salad/R2: "
                + ", ".join(missing)
            )
        upscaler = await asyncio.to_thread(
            create_optium_image_upscaler,
            base_url=settings.image_upscaler_url,
            salad_api_key=settings.salad_api_key,
            r2_endpoint_url=settings.r2_endpoint_url,
            r2_bucket=settings.r2_bucket,
            r2_access_key_id=settings.r2_access_key_id,
            r2_secret_access_key=settings.r2_secret_access_key,
            temp_dir=Path(settings.temp_dir) / "professional-image-finishing",
        )
    manifest = await asyncio.to_thread(
        generate_b2_images,
        output,
        plan,
        api_key=settings.openai_api_key,
        options=_image_options(),
    )
    # Crop only saved grid PNGs; this step cannot make a new paid API call.
    panel_assets = await asyncio.to_thread(crop_grid_assets, output, manifest)
    final_assets = (
        await asyncio.to_thread(
            finish_professional_images, output, plan, manifest,
            upscaler=upscaler, api_key=settings.openai_api_key,
        ) if professional else None
    )
    plan["image_assets"] = [
        {
            "block_id": item["block_id"],
            "beat_id": item["beat_id"],
            "file": item["file"],
            "sha256": item["sha256"],
            "provider": item.get("provider", "ai33"),
        }
        for item in (
            final_assets if final_assets is not None else
            [job for job in manifest["items"] if job.get("grid") is None] + panel_assets
        )
    ]
    # Keep the original full-resolution grid and panel coordinates for audit.
    plan["image_grids"] = [
        {
            "file": item["file"],
            "sha256": item["sha256"],
            "provider": item["provider"],
            "size": item["size"],
            "grid": item["grid"],
            "panels": item["panels"],
        }
        for item in manifest["items"] if item.get("grid") is not None
    ]
    _write(output / "visual_plan.json", plan)
    return manifest


def _continuity_retry_block_id(error: ImageGenerationError) -> int | None:
    """Return the B2 block implicated by a preflight continuity error."""
    if not isinstance(error.__cause__, GridPlanError):
        return None
    match = re.search(r"continuity numbering at (\d+)[A-Z]", str(error))
    return int(match.group(1)) if match else None


def _image_summary(manifest: dict[str, object]) -> dict[str, object]:
    items = manifest["items"]
    summary = {
        "status": "completed",
        "count": len(items),
        "model_id": manifest["model_id"],
        "openai_direct_count": sum(
            item.get("provider") == "openai_direct" for item in items
        ),
    }
    if manifest.get("finishing_status") == "completed":
        summary["professional_finished_count"] = len(manifest["final_assets"])
        summary["native_grid_video_edits"] = manifest.get("native_grid_video_calls", 0)
    if any(item.get("grid") is not None for item in items):
        summary["grid_count"] = sum(
            item.get("grid") is not None for item in items
        )
        summary["beat_count"] = manifest["total_described_beats"]
        summary["cropped_panel_count"] = len(manifest.get("panel_assets", []))
    return summary


async def _render_preview(
    output: Path,
    plan: dict[str, object],
    audio: dict[str, object],
    images: dict[str, object],
) -> dict[str, object]:
    """Join only verified saved assets; never start another paid generation."""
    return await asyncio.to_thread(generate_static_preview, output, plan, audio, images)


async def _preview_if_ready(
    output: Path,
    report: dict[str, object],
    *,
    plan: dict[str, object] | None = None,
    images: dict[str, object] | None = None,
) -> dict[str, object] | None:
    # Older grid manifests may still lack crops; never use a full grid as a
    # beat's start image. A completed crop map enables the normal preview.
    def missing_panels(saved: dict[str, object]) -> bool:
        expected = sum(
            sum(
                panel.get("source") != BLACK_PANEL_SOURCE
                for panel in item["panels"]
            )
            for item in saved.get("items", [])
            if item.get("grid") is not None
        )
        return expected > 0 and len(saved.get("panel_assets", [])) != expected

    if images is not None and missing_panels(images):
        return {
            "status": "deferred",
            "reason": "grid_panels_not_cropped",
            "grid_count": images["grid_count"],
        }
    run = report.get("run", {})
    audio = run.get("audio_generation") if isinstance(run, dict) else None
    if (
        not isinstance(audio, dict)
        or audio.get("status") != "completed"
        or audio.get("stt_complete") is not True
    ):
        return None
    if plan is None:
        visual_path = output / "visual_plan.json"
        if visual_path.is_symlink() or not visual_path.is_file():
            return None
        plan = json.loads(visual_path.read_text(encoding="utf-8"))
    if images is None:
        manifest_path = output / "images" / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return None
        images = json.loads(manifest_path.read_text(encoding="utf-8"))
    if images.get("status") != "completed":
        return None
    if plan.get("visual_style") == "profesional" and (
        images.get("finishing_status") != "completed"
    ):
        return {"status": "deferred", "reason": "professional_images_not_finished"}
    if missing_panels(images):
        return {
            "status": "deferred",
            "reason": "grid_panels_not_cropped",
            "grid_count": images["grid_count"],
        }
    return await _render_preview(output, plan, audio, images)


def _ltx_transport_configured() -> bool:
    return all((
        settings.postgres_dsn,
        settings.r2_endpoint_url,
        settings.r2_bucket,
        settings.r2_access_key_id,
        settings.r2_secret_access_key,
    ))


def _require_ltx_capacity_controller() -> None:
    """Fail before paid LTX submissions if the shared capacity leader is absent."""
    dsn = settings.salad_capacity_controller_postgres_dsn or settings.postgres_dsn
    try:
        health = read_capacity_controller_health(
            validate_capacity_controller_dsn(dsn or "")
        )
    except Exception as exc:
        raise RuntimeError(
            "Cannot verify Salad Capacity Controller health before LTX submission. "
            "Check POSTGRES_DSN / SALAD_CAPACITY_CONTROLLER_POSTGRES_DSN and run "
            "'uv run --locked --extra dev python "
            "scripts/salad/run_salad_capacity_controller.py --check-health'."
        ) from exc
    if not health.healthy:
        raise RuntimeError(
            f"Salad Capacity Controller is not healthy: {health.reason}. "
            "No new LTX inference job was submitted. Start the existing singleton "
            "controller as an independent service (SALAD_AUTOSCALER_ENABLED=true): "
            "'uv run --locked --extra dev python "
            "scripts/salad/run_salad_capacity_controller.py --poll-seconds 15'. "
            "Use '--check-health' to verify it before retrying --generate-video."
        )
    print(
        f"  Salad Capacity Controller healthy: id={health.controller_id} "
        f"status={health.status}; Postgres demand controls LTX replicas",
        flush=True,
    )


def _require_ltx_safe_a2v_worker() -> None:
    """Reject paid A2V v5 until its patched host-memory image is actually deployed."""
    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / "config" / "app.env", override=False)
    load_dotenv(repo_root / "config" / "salad.env", override=False)
    load_dotenv(repo_root / ".env", override=False)
    expected = os.getenv("SALAD_LTX25_PAGEABLE_A2V_IMAGE", "").strip()
    if not re.fullmatch(r"[^ ]+@sha256:[0-9a-fA-F]{64}", expected):
        raise RuntimeError(
            "Pinned-host fallback LTX A2V v5 requires SALAD_LTX25_PAGEABLE_A2V_IMAGE="
            "<repository>@sha256:<64-hex-digest> in config/salad.env. "
            "Build, publish and Prepare the new LTX worker first; "
            "do not resubmit the failed v4 job to an unpatched Docker image."
        )
    document = json.loads(
        (Path(__file__).resolve().parents[2] / "deploy" / "salad" / "services.json")
        .read_text(encoding="utf-8")
    )
    group_name = document["services"]["ltx25"]["group_name"]
    client = SaladClient(
        SaladDispatchConfig(
            api_key=settings.salad_api_key,
            api_base_url=os.getenv("SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL),
            organization_name=os.getenv("SALAD_ORGANIZATION"),
            project_name=os.getenv("SALAD_PROJECT"),
            container_group_name=group_name,
        )
    )
    configured_image = document["services"]["ltx25"]["image"]
    configured_repository = re.sub(r":[^/:]+$", "", configured_image)
    ensure_a2v_image(
        client=client,
        expected=expected,
        configured_repository=configured_repository,
        repo_root=repo_root,
        controller_dsn=(
            settings.salad_capacity_controller_postgres_dsn or settings.postgres_dsn
        ),
        auto_prepare=(
            os.getenv("SALAD_LTX_AUTO_PREPARE_ON_MISMATCH", "true").strip().lower()
            in {"true", "1", "yes", "on"}
        ),
    )


def _require_ltx_safe_i2v_worker() -> None:
    """Fail before paid I2V if Salad is not serving the reviewed FP8 resident image."""
    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / "config" / "app.env", override=False)
    load_dotenv(repo_root / "config" / "salad.env", override=False)
    load_dotenv(repo_root / ".env", override=False)
    expected = os.getenv("SALAD_LTX25_FP8_I2V_IMAGE", "").strip()
    if not re.fullmatch(r"[^ ]+@sha256:[0-9a-fA-F]{64}", expected):
        raise RuntimeError(
            "Promoted full-resolution FP8 no-offload I2V requires "
            "SALAD_LTX25_FP8_I2V_IMAGE=<repository>@sha256:<64-hex-digest> "
            "in config/salad.env. Prepare the reviewed standard I2V image "
            "before generating new I2V clips."
        )
    document = json.loads(
        (repo_root / "deploy" / "salad" / "services.json").read_text(encoding="utf-8")
    )
    definition = document["services"]["ltx25_i2v"]
    reviewed_repo = re.sub(r":[^/:]+$", "", definition["image"])
    if not expected.startswith(f"{reviewed_repo}@sha256:"):
        raise RuntimeError("Pinned FP8 I2V image must use the configured worker repository")
    client = SaladClient(
        SaladDispatchConfig(
            api_key=settings.salad_api_key,
            api_base_url=os.getenv("SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL),
            organization_name=os.getenv("SALAD_ORGANIZATION"),
            project_name=os.getenv("SALAD_PROJECT"),
            container_group_name=definition["group_name"],
        )
    )
    deployed = client.describe_container_group()
    container = deployed.get("container")
    actual = container.get("image") if isinstance(container, dict) else None
    if actual != expected:
        raise RuntimeError(
            "Salad LTX I2V worker image mismatch; no paid inference submitted. "
            f"Expected reviewed FP8 image {expected}, group reports {actual!r}. "
            "Prepare the exact digest for ltx25_i2v; leave A2V untouched."
        )


async def _video_if_ready(
    output: Path,
    report: dict[str, object],
    *,
    plan: dict[str, object] | None = None,
    images: dict[str, object] | None = None,
    require: bool = False,
) -> dict[str, object] | None:
    """Connect verified B2 PNGs and STT-aligned Fish narration to LTX."""
    run = report.get("run", {})
    audio = run.get("audio_generation") if isinstance(run, dict) else None
    if (
        not isinstance(audio, dict) or audio.get("status") != "completed"
        or audio.get("stt_complete") is not True
    ):
        if require:
            raise RuntimeError("LTX requires completed Fish audio with word-level STT")
        return None
    if plan is None:
        path = output / "visual_plan.json"
        if path.is_symlink() or not path.is_file():
            if require:
                raise RuntimeError("LTX requires the saved visual_plan.json")
            return None
        plan = json.loads(path.read_text(encoding="utf-8"))
    if images is None:
        path = output / "images" / "manifest.json"
        if path.is_symlink() or not path.is_file():
            if require:
                raise RuntimeError("LTX requires the completed B2 image manifest")
            return None
        images = json.loads(path.read_text(encoding="utf-8"))
    if images.get("status") != "completed":
        if require:
            raise RuntimeError("LTX requires completed B2 images")
        return None
    if plan.get("visual_style") == "profesional" and (
        images.get("finishing_status") != "completed"
    ):
        if require:
            raise RuntimeError("Professional LTX requires completed image x2/1080 finishing")
        return {"status": "deferred", "reason": "professional_images_not_finished"}
    if not _ltx_transport_configured():
        if require:
            raise RuntimeError("POSTGRES_DSN and all R2 credentials are required for LTX")
        return {"status": "deferred", "reason": "ltx_transport_not_configured"}

    def run_ltx() -> dict[str, object]:
        queue = PostgresJobQueueClient(dsn=settings.postgres_dsn)
        try:
            storage = create_r2_storage(
                endpoint_url=settings.r2_endpoint_url,
                bucket=settings.r2_bucket,
                access_key_id=settings.r2_access_key_id,
                secret_access_key=settings.r2_secret_access_key,
            )
            # Pydantic reads the config files but does not export Salad settings to os.environ.
            repo_root = Path(__file__).resolve().parents[2]
            load_dotenv(repo_root / "config" / "app.env", override=False)
            load_dotenv(repo_root / "config" / "salad.env", override=False)
            load_dotenv(repo_root / ".env", override=False)
            with SaladCostMeter(
                output,
                services_path=Path(__file__).resolve().parents[2]
                / "deploy" / "salad" / "services.json",
                api_key=settings.salad_api_key,
                organization=os.getenv("SALAD_ORGANIZATION"),
                project=os.getenv("SALAD_PROJECT"),
            ):
                cta_dir = (
                    Path(run["cta_cache_dir"])
                    if isinstance(run, dict) and isinstance(run.get("cta_cache_dir"), str)
                    and (output / "CTA" / "output.json").is_file()
                    else None
                )
                if isinstance(run, dict) and run.get("casero_cta_enabled") is True:
                    if cta_dir is None:
                        raise RuntimeError(
                            "Casero CTA assets or placement missing; reconcile before LTX"
                        )
                    from ai_video_factory.workflows.casero_cta_video import cta_assets

                    cached = cta_assets(cta_dir)
                    if (
                        cached["avatar_sha256"] != hashlib.sha256(
                            (output / "avatar.png").read_bytes()
                        ).hexdigest()
                        or cached["qr_sha256"] != hashlib.sha256(
                            (output / "qr.png").read_bytes()
                        ).hexdigest()
                        or cached["voice_id"] != _run_voice_id(output)
                        or cached.get("voice_provider", "fishaudio") != (
                            _run_voice_provider(output)
                        )
                    ):
                        raise RuntimeError(
                            "Cached casero CTA identity differs from this run's avatar, QR or voice"
                        )
                return generate_b2_ltx_video(
                    output, plan, audio, images, casero_cta_dir=cta_dir,
                    queue=queue, storage=storage,
                    poll_seconds=settings.inference_client_poll_seconds,
                    timeout_seconds=max(settings.inference_client_timeout_seconds, 21600.0),
                    check_capacity=_require_ltx_capacity_controller,
                    check_a2v_worker=_require_ltx_safe_a2v_worker,
                    check_i2v_worker=_require_ltx_safe_i2v_worker,
                )
        finally:
            queue.close()

    return await asyncio.to_thread(run_ltx)


def _preflight_stt_alignment(
    output: Path,
    plan: dict[str, object],
    audio: dict[str, object] | None,
) -> dict[str, object] | None:
    """Validate saved STT against B2 before starting any paid image requests."""
    if (
        not isinstance(audio, dict)
        or audio.get("status") != "completed"
        or audio.get("stt_complete") is not True
    ):
        return None
    timeline = build_stt_beat_timeline(output, plan, audio)
    return {
        "status": "completed",
        "block_count": len(plan.get("blocks", [])),
        "beat_count": len(timeline["beats"]),
    }


async def _preflight_stt_alignment_with_retries(
    output: Path,
    plan: dict[str, object],
    audio: dict[str, object] | None,
    *,
    allow_audio_regeneration: bool = True,
) -> dict[str, object] | None:
    """Retry STT twice per audio version, then regenerate that block up to 3 times."""
    if (
        not isinstance(audio, dict)
        or audio.get("status") != "completed"
        or audio.get("stt_complete") is not True
    ):
        return None
    stt_client: OpenAISttClient | None = None
    while True:
        try:
            return _preflight_stt_alignment(output, plan, audio)
        except SttAlignmentError as exc:
            if stt_client is None:
                stt_client = OpenAISttClient(api_key=settings.openai_api_key)
            try:
                attempt = await retranscribe_audio_block(
                    output,
                    audio,
                    exc.block_id,
                    stt_client=stt_client,
                )
            except SttRetryLimitReached as limit_exc:
                if not allow_audio_regeneration:
                    raise exc from limit_exc
                try:
                    regeneration = await regenerate_audio_block(
                        output,
                        audio,
                        exc.block_id,
                        speech_client=AI33SpeechClient(settings.ai33_api_key),
                        stt_client=stt_client,
                        poll_timeout_seconds=settings.ai33_fish_poll_timeout_seconds,
                        poll_interval_seconds=settings.ai33_fish_poll_interval_seconds,
                    )
                except AudioRegenerationLimitReached as limit_exc:
                    raise exc from limit_exc
                print(
                    f"  STT still mismatched in block {exc.block_id}; "
                    f"regenerated its audio {regeneration}/3 times and transcribed it.",
                    flush=True,
                )
                continue
            except Exception as retry_error:
                raise retry_error from exc
            print(
                f"  STT alignment mismatch in block {exc.block_id}; "
                f"STT attempt {attempt}/2 completed on the same audio.",
                flush=True,
            )


async def _preflight_saved_run_audio(
    output: Path,
    plan: dict[str, object],
    run_metadata: dict[str, object],
) -> bool:
    """Validate/recover a completed audio artifact already bound to this run."""
    saved_audio = run_metadata.get("audio_generation")
    if (
        not isinstance(saved_audio, dict)
        or saved_audio.get("status") != "completed"
        or saved_audio.get("stt_complete") is not True
    ):
        return False
    # Keep Casero's established call signature and retry policy unchanged.
    # Professional Gemini intentionally forbids implicit paid Fish regeneration.
    options = (
        {"allow_audio_regeneration": False}
        if saved_audio.get("provider") == "gemini" else {}
    )
    run_metadata["stt_alignment"] = await _preflight_stt_alignment_with_retries(
        output, plan, saved_audio, **options,
    )
    return True


async def _run_images_only(
    script_file: Path, output_root: Path, *, no_video: bool = False,
    results_root: Path = Path("data/results"),
    managed_target: Path | None = None,
) -> None:
    """Resume image tasks using B2 artifacts without repeating any planning calls."""
    root = output_root.resolve()
    source = script_file.resolve()
    cwd = Path.cwd().resolve()
    if root == cwd or root in cwd.parents or source.is_relative_to(root):
        raise SystemExit("--output must be a dedicated output root outside the input directory")
    output = output_root / script_file.stem
    report_path = output / "run_report.json"
    plan_path = output / "visual_plan.json"
    if (
        output.is_symlink()
        or output.is_junction()
        or report_path.is_symlink()
        or plan_path.is_symlink()
        or not report_path.is_file()
        or not plan_path.is_file()
        or not (output / "B2" / "merged_output.json").is_file()
    ):
        raise SystemExit(
            "Cannot resume standard OpenAI images without a complete saved B2 visual plan"
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    source_sha = hashlib.sha256(script_file.read_bytes()).hexdigest()
    if report.get("run", {}).get("script_sha256") != source_sha:
        raise SystemExit("The selected script differs from the saved B2 image source")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    try:
        report["run"]["stt_alignment"] = await _preflight_stt_alignment_with_retries(
            output,
            plan,
            report.get("run", {}).get("audio_generation"),
            allow_audio_regeneration=False,
        ) or {"status": "skipped"}
    except Exception as exc:
        report["run"]["stt_alignment"] = {"status": "failed", "error": str(exc)}
        report["run"]["status"] = "failed"
        report.setdefault("timings", {})["status"] = "failed"
        report.setdefault("api_costs", {})["run_status"] = "failed"
        _refresh_audio_costs(report, output)
        _write(report_path, report)
        raise
    report["run"]["video_preview"] = {"status": "stale_image_regeneration"}
    report["run"]["video_generation"] = {"status": "stale_image_regeneration"}
    report["run"]["image_generation"] = {"status": "running"}
    report["run"]["status"] = "running"
    report.setdefault("timings", {})["status"] = "running"
    _refresh_audio_costs(report, output)
    _write(report_path, report)
    try:
        removed_images = await asyncio.to_thread(clear_b2_images_for_rerun, output)
        if removed_images:
            print(f"Removed {removed_images} previous B2 image(s).", flush=True)
        manifest = await _generate_images(output, plan)
    except Exception:
        report["run"]["image_generation"] = {"status": "incomplete"}
        report["run"]["status"] = "failed"
        report.setdefault("timings", {})["status"] = "failed"
        _refresh_audio_costs(report, output)
        _write(report_path, report)
        raise
    report["run"]["image_generation"] = _image_summary(manifest)
    _refresh_audio_costs(report, output)
    _write(report_path, report)
    if managed_target is not None:
        await asyncio.to_thread(sync_managed_outputs, output, managed_target)
        print(f"  Images published: {managed_target / 'media' / 'images'}", flush=True)
    try:
        preview = await _preview_if_ready(output, report, plan=plan, images=manifest)
    except Exception:
        report["run"]["video_preview"] = {"status": "incomplete"}
        report["run"]["status"] = "failed"
        _refresh_audio_costs(report, output)
        _write(report_path, report)
        raise
    if preview is not None:
        report["run"]["video_preview"] = preview
        _write(report_path, report)
        if managed_target is not None:
            await asyncio.to_thread(sync_managed_outputs, output, managed_target)
    if no_video:
        report["run"]["video_generation"] = {"status": "skipped"}
    else:
        try:
            video = await _video_if_ready(output, report, plan=plan, images=manifest)
        except Exception:
            report["run"]["video_generation"] = {"status": "incomplete"}
            report["run"]["status"] = "failed"
            _refresh_audio_costs(report, output)
            _write(report_path, report)
            raise
        if video is not None:
            report["run"]["video_generation"] = video
    report["run"]["status"] = "completed"
    report["timings"]["status"] = "completed"
    _refresh_audio_costs(report, output)
    _write(report_path, report)
    await asyncio.to_thread(
        _publish_if_ready, output, script_file, results_root, managed_target
    )
    image_stage = report.get("api_costs", {}).get("stages", {}).get("B2 images", {})
    price = image_stage.get("estimated_cost_usd")
    print(
        f"  B2 images cost: ${price}" if price is not None
        else "  B2 images cost: coste no disponible",
        flush=True,
    )
    print(
        f"  Images complete: {len(manifest['items'])} PNG "
        f"({manifest.get('openai_direct_count', 0)} standard OpenAI requests)",
        flush=True,
    )
    _print_observed_salad_total(report["api_costs"])



async def _run_videos_only(
    script_file: Path, output_root: Path,
    *, results_root: Path = Path("data/results"),
    managed_target: Path | None = None,
) -> None:
    """Resume durable LTX jobs without repeating OpenAI images, bots or Fish TTS."""
    root = output_root.resolve()
    source = script_file.resolve()
    cwd = Path.cwd().resolve()
    if root == cwd or root in cwd.parents or source.is_relative_to(root):
        raise SystemExit("--output must be outside the repository and source directory")
    output = output_root / script_file.stem
    report_path = output / "run_report.json"
    plan_path = output / "visual_plan.json"
    if (
        output.is_symlink() or output.is_junction()
        or report_path.is_symlink() or plan_path.is_symlink()
        or not report_path.is_file() or not plan_path.is_file()
    ):
        raise SystemExit("--generate-video requires a saved B2 plan and run report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("run", {}).get("script_sha256") != hashlib.sha256(
        source.read_bytes()
    ).hexdigest():
        raise SystemExit("Script differs from the saved LTX input")
    if report.get("run", {}).get("stt_alignment", {}).get("status") != "completed":
        raise SystemExit("--generate-video requires previously validated B2/STT alignment")
    preview = report.get("run", {}).get("video_preview")
    if not isinstance(preview, dict) or preview.get("status") != "completed":
        raise SystemExit("--generate-video requires an existing completed preview")
    try:
        _verified_asset(output, preview.get("file"), preview.get("sha256"))
    except StaticPreviewError as exc:
        raise SystemExit(f"Saved preview is missing or invalid: {exc}") from exc
    print(
        "  Saved B2 plan, Fish/STT alignment and preview verified; "
        "preparing LTX I2V/A2V jobs",
        flush=True,
    )
    _refresh_audio_costs(report, output)
    try:
        video = await _video_if_ready(output, report, require=True)
    except Exception:
        report["run"]["video_generation"] = {"status": "incomplete"}
        report["run"]["status"] = "failed"
        _refresh_audio_costs(report, output)
        _write(report_path, report)
        raise
    report["run"]["video_generation"] = video
    report["run"]["status"] = "completed"
    report["timings"]["status"] = "completed"
    _refresh_audio_costs(report, output)
    _write(report_path, report)
    await asyncio.to_thread(
        _publish_if_ready, output, script_file, results_root, managed_target
    )
    print(f"  LTX video ready: {video['file']}", flush=True)
    _print_observed_salad_total(report["api_costs"])




def _run_voice_id(output: Path) -> str:
    """Use the immutable voice snapshot of this run, including audio-only resumes."""
    path = output / "avatar_voice.json"
    if path.is_symlink():
        raise SystemExit("No se permiten enlaces simbólicos en avatar_voice.json.")
    if path.is_file():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            voice_id = record["voice_id"]
            if not isinstance(voice_id, str) or not voice_id.strip():
                raise ValueError("El Voice ID de la generación todavía no está listo.")
            return voice_id
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"Configuración de voz del run inválida: {path}") from exc
    # Older runs did not have per-avatar voice snapshots.
    return settings.ai33_fish_voice_id


def _run_voice_provider(output: Path) -> str:
    """Treat legacy voice snapshots as Fish and reject unknown saved providers."""
    path = output / "avatar_voice.json"
    if path.is_symlink():
        raise SystemExit("El snapshot de voz es un enlace no permitido.")
    if not path.exists():
        return "fishaudio"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            raise ValueError("El snapshot de voz no es un objeto.")
        return valid_voice_provider(snapshot.get("provider", "fishaudio"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Proveedor de voz del run inválido: {path}") from exc


async def _generate_audio(
    script: str,
    output: Path,
    *,
    provider: OpenAIProvider | None = None,
    b11: B11Output,
    max_parallel_calls: int = 8,
    resume: bool = False,
) -> dict[str, object]:
    return await generate_fish_audio(
        script,
        output,
        b11=b11,
        max_parallel_calls=max_parallel_calls,
        director_model=settings.openai_model,
        provider=provider,
        ai33_api_key=settings.ai33_api_key,
        voice_id=_run_voice_id(output),
        voice_provider=_run_voice_provider(output),
        speed=settings.ai33_fish_speed,
        poll_timeout_seconds=settings.ai33_fish_poll_timeout_seconds,
        poll_interval_seconds=settings.ai33_fish_poll_interval_seconds,
        resume=resume,
        stt_client=OpenAISttClient(api_key=settings.openai_api_key),
    )



def _attach_salad_costs(
    api_costs: dict[str, object], output: Path
) -> dict[str, object] | None:
    """Keep the OpenAI-only total intact; add the observed Salad subtotal."""
    salad = read_salad_costs(output / "video" / "salad_costs.json")
    if salad is None:
        return None
    observed = salad.get("priced_subtotal_usd")
    if observed is None:
        api_costs["priced_subtotal_including_salad_usd"] = None
    else:
        subtotal = Decimal(str(api_costs["priced_subtotal_usd"])) + Decimal(str(observed))
        api_costs["priced_subtotal_including_salad_usd"] = f"{subtotal:.8f}"
    api_total = api_costs.get("estimated_total_usd")
    gpu_total = salad.get("estimated_observed_cost_usd")
    api_costs["estimated_total_including_salad_usd"] = (
        f"{Decimal(str(api_total)) + Decimal(str(gpu_total)):.8f}"
        if api_total is not None and gpu_total is not None
        else None
    )
    return salad


def _print_observed_salad_total(api_costs: dict[str, object]) -> None:
    combined = api_costs.get("priced_subtotal_including_salad_usd")
    if combined is not None:
        print(
            "  OpenAI + observed Salad subtotal: $" + str(combined)
            + " (provisional; Salad outside the video stage is not included)",
            flush=True,
        )


def _refresh_audio_costs(report: dict[str, object], output: Path) -> None:
    """Reconcile paid Fish director calls without repeating earlier B charges."""
    previous = report.get("api_costs")
    if not isinstance(previous, dict) or not isinstance(previous.get("requests"), list):
        return
    ledger = ApiCostLedger()
    ledger.restore_requests(previous["requests"])
    ledger.sync_professional_voice_usage(output)
    ledger.sync_fish_director_usage(output)
    ledger.sync_image_usage(output)
    stages = previous.get("stages")
    if not isinstance(stages, dict):
        return
    expected = {
        stage: stages[stage]["expected_calls"]
        for stage in _STAGES
        if isinstance(stages.get(stage), dict)
    }
    run = report.get("run")
    state = run.get("status") if isinstance(run, dict) else None
    audio = run.get("audio_generation") if isinstance(run, dict) else None
    if isinstance(audio, dict) and audio.get("status") == "incomplete":
        state = "failed"
    if state not in ("running", "completed", "failed"):
        state = "running"
    report["api_costs"] = ledger.report(
        blocks=None, run_status=state, expected_calls=expected,
    )
    salad = _attach_salad_costs(report["api_costs"], output)
    if salad is not None:
        report["salad_costs"] = salad


def _activate_pending_avatar_clone(
    output: Path, report: dict[str, object]
) -> None:
    """Resolve a deferred avatar clone before the first audio-only generation."""
    snapshot_path = output / "avatar_voice.json"
    if snapshot_path.is_symlink() or not snapshot_path.is_file():
        return
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            raise ValueError("Voice snapshot is not an object")
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Snapshot de voz inválido: {snapshot_path}") from exc
    if snapshot.get("mode") != "clone" or snapshot.get("voice_id") is not None:
        return
    run = report.get("run")
    avatar = run.get("avatar") if isinstance(run, dict) else None
    source = avatar.get("source") if isinstance(avatar, dict) else None
    if not isinstance(source, str) or not source:
        raise SystemExit("Falta la ubicación original del avatar para clonar su voz.")
    avatar_file = Path(source)
    if not avatar_file.is_file():
        # Older run reports may point to the PNG before it was organized.
        migrated = avatar_file.parent / avatar_file.stem / avatar_file.name
        if not migrated.is_file():
            raise SystemExit(f"El PNG original del avatar ya no está disponible: {source}")
        avatar_file = migrated
    avatars_dir = (
        avatar_file.parent.parent
        if avatar_file.parent.name == avatar_file.stem
        else avatar_file.parent
    )
    avatar_file = organize_avatar(avatar_file, avatars_dir)
    pending = ensure_avatar_voice(
        avatar_file=avatar_file,
        avatars_dir=avatars_dir,
        default_voice_id=settings.ai33_fish_voice_id,
        activate=False,
    )
    if (
        pending.mode != "clone"
        or pending.audio_filename != snapshot.get("audio_filename")
        or pending.audio_sha256 != snapshot.get("audio_sha256")
    ):
        raise SystemExit("La voz del avatar cambió desde el inicio del run.")
    active = ensure_avatar_voice(
        avatar_file=avatar_file,
        avatars_dir=avatars_dir,
        default_voice_id=settings.ai33_fish_voice_id,
        api_key=settings.ai33_api_key,
    )
    if active.voice_id is None:
        raise SystemExit("La voz clonada aún no tiene Voice ID.")
    ready = active.snapshot()
    _write(snapshot_path, ready)
    avatar["voice"] = ready
    _write(output / "run_report.json", report)


async def _run_audio_only(
    script_file: Path,
    output_root: Path,
    *,
    resume: bool,
    no_video: bool = False,
    results_root: Path = Path("data/results"),
    managed_target: Path | None = None,
) -> None:
    """An independent audio-only run: no avatar, B bots, B2 images or GPUs."""
    root = output_root.resolve()
    source = script_file.resolve()
    cwd = Path.cwd().resolve()
    if root == cwd or root in cwd.parents or source.is_relative_to(root):
        raise SystemExit("--output must be outside the repository and source directory")
    output = output_root / script_file.stem
    report_path = output / "run_report.json"
    if output.is_symlink() or report_path.is_symlink() or not report_path.is_file():
        raise SystemExit("Audio-only mode requires the saved run_report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    raw = script_file.read_bytes()
    try:
        script = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("Audio source must be UTF-8") from exc
    if report.get("run", {}).get("script_sha256") != hashlib.sha256(raw).hexdigest():
        raise SystemExit("Script changed since the B run; audio-only mode is blocked")
    is_professional = report.get("run", {}).get("visual_style") == "profesional"
    if not resume and not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required to rerun the voice director")
    if is_professional:
        if not settings.gemini_api_key:
            raise SystemExit("GEMINI_API_KEY is required for professional Gemini TTS")
    elif not settings.ai33_api_key:
        raise SystemExit("AI33_API_KEY is required for Casero voice synthesis")
    b11_path = output / "B1.1" / "output.json"
    if b11_path.is_symlink() or not b11_path.is_file():
        raise SystemExit("Audio-only mode requires the saved validated B1.1 output")
    try:
        b11 = B11Output.model_validate(json.loads(b11_path.read_text(encoding="utf-8")))
        if b11.error is not None or b11.blocks is None or b11.narrative_core is None:
            raise ValueError("B1.1 checkpoint is not a successful output")
        materialize_blocks(script, b11.blocks)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Invalid B1.1 audio checkpoint: {exc}") from exc

    # Audio-only runs must restore and extend the same V timing ledger as
    # complete B runs. Paid token usage is reconciled from durable V states.
    audio_started = perf_counter()
    prior_timing = TimingLedger()
    prior_elapsed = prior_timing.restore(report["timings"]) if is_professional else 0.0

    def finish_audio_timing() -> None:
        if not is_professional:
            return
        status = report["run"].get("status", "running")
        if status not in {"running", "completed", "failed"}:
            status = "running"
        report["timings"] = prior_timing.report(
            run_seconds=prior_elapsed + perf_counter() - audio_started,
            status=status,
        )

    previous_audio = report["run"].get("audio_generation")
    previous_sha = (
        previous_audio.get("sha256") if isinstance(previous_audio, dict) else None
    )
    if not is_professional:
        _activate_pending_avatar_clone(output, report)

    provider = (
        None
        if resume and not is_professional
        else OpenAIProvider(
            api_key=settings.openai_api_key,
            reasoning_effort=settings.openai_reasoning_effort,
            service_tier="default",
        )
    )
    try:
        reused_audio = False
        artifact = None
        if resume and not unfinished_audio_runs(output):
            artifact = _latest_completed_audio(output, hashlib.sha256(raw).hexdigest())
            reused_audio = artifact is not None
        if artifact is None:
            if is_professional:
                merged_path = output / "B2" / "merged_output.json"
                if merged_path.is_symlink() or not merged_path.is_file():
                    raise RuntimeError("Professional audio requires a saved validated B2 result")
                merged = json.loads(merged_path.read_text(encoding="utf-8"))
                blocks = merged.get("blocks")
                core = merged.get("narrative_core")
                if not isinstance(blocks, list) or not blocks or not isinstance(core, dict):
                    raise RuntimeError("Professional B2 merged checkpoint is incomplete")
                for block in blocks:
                    ProfessionalB2Output.model_validate({
                        "pipeline_stage": "B2", "narrative_core": core,
                        "blocks": [block], "error": None,
                    })
                pending = unfinished_audio_runs(output)
                if resume and len(pending) != 1:
                    raise RuntimeError(
                        "Professional --resume-audio requires one pending Gemini task"
                    )
                if pending and not resume:
                    raise RuntimeError(
                        "An incomplete paid audio task exists; reconcile before regeneration"
                    )
                nonce = (
                    json.loads(pending[0].read_text(encoding="utf-8")).get("generation_nonce", "")
                    if resume else uuid4().hex
                )
                if resume and not pending[0].parent.name.startswith("gemini-"):
                    raise RuntimeError("Pending audio task is not a professional Gemini run")
                artifact = await generate_professional_audio(
                    core=core, blocks=blocks, output=output,
                    provider=provider, model=settings.openai_model,
                    gemini_api_key=settings.gemini_api_key,
                    voice_id=_run_voice_id(output),
                    source_sha256=hashlib.sha256(raw).hexdigest(),
                    generation_nonce=nonce,
                    on_timing=prior_timing.record_call,
                    on_stage_duration=prior_timing.record_stage,
                    max_parallel_calls=8,
                )
            else:
                artifact = await _generate_audio(
                    script, output, b11=b11, provider=provider, resume=resume
                )
    except Exception:
        report["run"]["audio_generation"] = {"status": "incomplete"}
        report["run"]["status"] = "failed"
        finish_audio_timing()
        _refresh_audio_costs(report, output)
        _write(report_path, report)
        raise
    if artifact.get("sha256") is not None and artifact["sha256"] != previous_sha:
        # Only a verified changed narration invalidates prior preview/LTX state.
        report["run"]["video_preview"] = {"status": "stale_audio_regeneration"}
        report["run"]["video_generation"] = {"status": "stale_audio_regeneration"}
    report["run"]["audio_generation"] = artifact
    finish_audio_timing()
    _refresh_audio_costs(report, output)
    _write(report_path, report)
    if managed_target is not None:
        await asyncio.to_thread(sync_managed_outputs, output, managed_target)
        print(
            f"  Narration and block audio published: {managed_target / 'media' / 'audio'}",
            flush=True,
        )
    plan_path = output / "visual_plan.json"
    if plan_path.is_file() and not plan_path.is_symlink():
        try:
            report["run"]["stt_alignment"] = await _preflight_stt_alignment_with_retries(
                output,
                json.loads(plan_path.read_text(encoding="utf-8")),
                artifact,
                allow_audio_regeneration=artifact.get("provider") != "gemini",
            ) or {"status": "skipped"}
        except Exception as exc:
            report["run"]["stt_alignment"] = {"status": "failed", "error": str(exc)}
            report["run"]["status"] = "failed"
            report["timings"]["status"] = "failed"
            report["api_costs"]["run_status"] = "failed"
            report["run"]["video_preview"] = {"status": "blocked_by_stt_alignment"}
            _refresh_audio_costs(report, output)
            _write(report_path, report)
            raise
        _write(report_path, report)
    try:
        preview = await _preview_if_ready(output, report)
    except Exception:
        report["run"]["video_preview"] = {"status": "incomplete"}
        _refresh_audio_costs(report, output)
        _write(report_path, report)
        raise
    if preview is not None:
        report["run"]["video_preview"] = preview
        run = report.get("run", {})
        if (
            isinstance(run, dict)
            and run.get("audio_generation", {}).get("status") == "completed"
            and run.get("image_generation", {}).get("status") == "completed"
            and run.get("stt_alignment", {}).get("status") == "completed"
        ):
            run["status"] = "completed"
            report.setdefault("timings", {})["status"] = "completed"
            report.setdefault("api_costs", {})["run_status"] = "completed"
    if no_video:
        report["run"]["video_generation"] = {"status": "skipped"}
    else:
        try:
            video = await _video_if_ready(output, report)
        except Exception:
            report["run"]["video_generation"] = {"status": "incomplete"}
            report["run"]["status"] = "failed"
            _refresh_audio_costs(report, output)
            _write(report_path, report)
            raise
        if video is not None:
            report["run"]["video_generation"] = video
    finish_audio_timing()
    _refresh_audio_costs(report, output)
    _write(report_path, report)
    await asyncio.to_thread(
        _publish_if_ready, output, script_file, results_root, managed_target
    )
    if is_professional and not reused_audio:
        for voice_stage in ("V1", "V2", "V3"):
            duration = report["timings"]["stages"][voice_stage]["elapsed_seconds"]
            if duration is not None:
                _print_metric(
                    voice_stage, duration,
                    report["api_costs"]["stages"][voice_stage]["estimated_cost_usd"],
                )
    display_path = (
        managed_target / "media" / "audio" / "narracion.wav"
        if managed_target is not None else output / artifact["file"]
    )
    print(
        f"  {'Reused' if reused_audio else 'Narration ready'}: {display_path} "
        f"(voice {artifact['voice_id']})",
        flush=True,
    )
    _print_observed_salad_total(report["api_costs"])


def _latest_completed_audio(output: Path, source_sha256: str) -> dict[str, object] | None:
    """Load a verified completed audio run when resume has nothing pending."""
    latest_path = output / "audio" / "latest.json"
    if latest_path.is_symlink():
        raise SystemExit("Refusing linked audio/latest.json")
    if not latest_path.is_file():
        return None
    artifact = json.loads(latest_path.read_text(encoding="utf-8"))
    if not isinstance(artifact, dict) or artifact.get("status") != "completed":
        return None
    run_id = artifact.get("run_id")
    if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id:
        return None
    state_path = output / "audio" / "runs" / run_id / "task.json"
    if state_path.is_symlink() or not state_path.is_file():
        return None
    task = json.loads(state_path.read_text(encoding="utf-8"))
    if (
        task.get("status") != "completed"
        or task.get("run_id") != run_id
        or task.get("source_sha256") != source_sha256
        or task.get("audio") != artifact
    ):
        return None
    relative = artifact.get("file")
    digest = artifact.get("sha256")
    if not isinstance(relative, str) or not isinstance(digest, str):
        return None
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    audio_path = output / relative_path
    if (
        audio_path.is_symlink()
        or not audio_path.is_file()
        or not audio_path.resolve().is_relative_to(output.resolve())
    ):
        return None
    with audio_path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            return None
    # Never resume a supposedly completed narration if any paid block file
    # referenced by the saved artifact was lost. Keep its task state intact
    # for explicit recovery; do not submit duplicate paid work.
    blocks = artifact.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return None
    for block in blocks:
        if not isinstance(block, dict):
            return None
        name, sha = block.get("file"), block.get("sha256")
        if not isinstance(name, str) or not isinstance(sha, str):
            return None
        relative_block = Path(name)
        if relative_block.is_absolute() or ".." in relative_block.parts:
            return None
        source = output / relative_block
        if (
            source.is_symlink() or not source.is_file()
            or not source.resolve().is_relative_to(output.resolve())
        ):
            return None
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                return None
    return artifact


class BotArtifacts:
    """Persist the exact request payload and validated response of every bot call."""

    def __init__(self, output: Path) -> None:
        self._output = output

    def _folder(self, stage: str, block_id: int | None) -> Path:
        if stage not in (*_STAGES, "CTA"):
            raise ValueError(f"Unknown B pipeline stage: {stage}")
        if stage in {"B1.1", "CTA"}:
            if block_id is not None:
                raise ValueError(f"{stage} must not have a block ID")
            return self._output / stage
        if block_id is None or block_id < 1:
            raise ValueError(f"{stage} requires a positive block ID")
        return self._output / stage / f"block_{block_id}"

    def input(self, stage: str, block_id: int | None, payload: dict[str, object]) -> None:
        directory = self._folder(stage, block_id)
        original = directory / "input.json"
        if not original.is_file():
            _write(original, payload)
            return
        attempt = 2
        while (directory / f"input_attempt_{attempt}.json").exists():
            attempt += 1
        _write(directory / f"input_attempt_{attempt}.json", payload)

    def output(self, stage: str, block_id: int | None, payload: dict[str, object]) -> None:
        _write(self._folder(stage, block_id) / "output.json", payload)

    def rejected_b11(
        self, attempt: int, error: str, payload: dict[str, object]
    ) -> None:
        _write(
            self._folder("B1.1", None) / f"rejected_attempt_{attempt}.json",
            {"attempt": attempt, "validation_error": error, "response": payload},
        )

    def rejected_b12(
        self, block_id: int, attempt: int, error: str, payload: dict[str, object]
    ) -> None:
        _write(
            self._folder("B1.2", block_id) / f"rejected_attempt_{attempt}.json",
            {"attempt": attempt, "validation_error": error, "response": payload},
        )

    def rejected_b2(
        self, block_id: int, attempt: int, error: str, payload: dict[str, object]
    ) -> None:
        _write(
            self._folder("B2", block_id) / f"rejected_attempt_{attempt}.json",
            {"attempt": attempt, "validation_error": error, "response": payload},
        )


class TimingLedger:
    """Monotonic wall-clock timing; parallel calls are not added into stage wall time."""

    def __init__(self) -> None:
        self._stages: dict[str, float] = {}
        self._calls: list[dict[str, object]] = []

    def record_stage(self, stage: str, elapsed_seconds: float) -> None:
        if stage not in _METRIC_STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        self._stages[stage] = self._stages.get(stage, 0.0) + elapsed_seconds

    def record_call(self, stage: str, block_id: int | None, elapsed_seconds: float) -> None:
        if stage not in _METRIC_STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        self._calls.append(
            {
                "stage": stage,
                "block_id": block_id,
                "elapsed_seconds": round(elapsed_seconds, 3),
            }
        )

    def call_count(self, stage: str) -> int:
        return sum(item["stage"] == stage for item in self._calls)

    def restore(self, report: dict[str, object]) -> float:
        """Restore stage timing from a prior report; return its total elapsed time."""
        stages = report.get("stages")
        calls = report.get("calls")
        if not isinstance(stages, dict) or not isinstance(calls, list):
            raise ValueError("Cannot restore malformed timing history")
        for stage in _METRIC_STAGES:
            entry = stages.get(stage)
            if isinstance(entry, dict) and isinstance(entry.get("elapsed_seconds"), (int, float)):
                self._stages[stage] = float(entry["elapsed_seconds"])
        for call in calls:
            if (
                not isinstance(call, dict)
                or call.get("stage") not in _METRIC_STAGES
                or not isinstance(call.get("elapsed_seconds"), (int, float))
            ):
                raise ValueError("Cannot restore malformed timing call history")
            self._calls.append(dict(call))
        elapsed = report.get("run_elapsed_seconds")
        if not isinstance(elapsed, (int, float)):
            raise ValueError("Cannot restore missing total run time")
        return float(elapsed)

    def report(self, *, run_seconds: float, status: str) -> dict[str, object]:
        calls = sorted(
            self._calls,
            key=lambda item: (_METRIC_STAGES.index(str(item["stage"])), item["block_id"] or 0),
        )
        return {
            "unit": "seconds",
            "status": status,
            "run_elapsed_seconds": round(run_seconds, 3),
            "stages": {
                stage: {
                    "elapsed_seconds": (
                        round(self._stages[stage], 3) if stage in self._stages else None
                    ),
                    "call_count": len([item for item in calls if item["stage"] == stage]),
                    "sum_call_seconds": round(
                        sum(
                            float(item["elapsed_seconds"])
                            for item in calls
                            if item["stage"] == stage
                        ),
                        3,
                    ),
                }
                for stage in _METRIC_STAGES
            },
            "calls": calls,
        }


def _print_metric(name: str, seconds: float, cost: object) -> None:
    """Print a single completed bot/run line, flushing as soon as it finishes."""
    price = f"${cost}" if cost is not None else "coste no disponible"
    print(f"  {name} total: {seconds:.3f} s {price}", flush=True)


async def main() -> None:
    args = parse_args()
    results_root = getattr(args, "results_root", Path("data/results"))
    # Git does not track empty runtime directories: restore the local libraries.
    if args.script_file is None:
        args.scripts_dir.mkdir(parents=True, exist_ok=True)
    args.avatars_dir.mkdir(parents=True, exist_ok=True)

    if args.list_scripts:
        scripts = available_scripts(args.scripts_dir)
        if not scripts:
            print(f"No .txt scripts found in {args.scripts_dir}.")
        else:
            print(f"Available scripts in {args.scripts_dir}:")
            for path in scripts:
                print(f"  {path.name}")
        return

    if args.list_avatars:
        avatars = available_avatars(args.avatars_dir)
        if not avatars:
            print(f"No PNG avatars found in {args.avatars_dir}.")
        else:
            print(f"Available avatars in {args.avatars_dir}:")
            for path in avatars:
                print(f"  {path.name}")
        return

    if getattr(args, "qr_only", False):
        avatar_file = select_avatar(avatars_dir=args.avatars_dir, avatar_name=args.avatar)
        load_avatar_png(avatar_file)
        avatar_file = _recover_avatar_layout(avatar_file, args.avatars_dir)
        qr = ensure_avatar_qr(
            avatar_file=avatar_file,
            avatars_dir=args.avatars_dir,
            input_url=getattr(args, "qr_url", None),
        )
        avatar_file = organize_avatar(avatar_file, args.avatars_dir)
        print(f"QR listo: {avatar_file.parent / 'qr.png'}")
        print(f"URL: {qr.url}")
        return

    script_file = select_script(
        scripts_dir=args.scripts_dir,
        script_name=args.script,
        script_file=args.script_file,
    )
    if getattr(args, "youtube_only", False):
        output_root = args.output if args.output is not None else settings.output_dir
        output = output_root / script_file.stem
        if not output.is_dir() or output.is_symlink() or output.is_junction():
            raise SystemExit("No saved completed run for YouTube-only distribution")
        try:
            published = await asyncio.to_thread(
                _publish_if_ready,
                output, script_file, results_root,
                getattr(args, "managed_target", None),
                fail_on_youtube_error=True,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None
        if published is None:
            raise SystemExit(
                "YouTube-only requires a completed, previously verified final MP4"
            )
        return
    if getattr(args, "publish_only", False):
        output_root = args.output if args.output is not None else settings.output_dir
        output = output_root / script_file.stem
        if not output.is_dir() or output.is_symlink() or output.is_junction():
            raise SystemExit(f"No saved pipeline output for publishing: {output}")
        managed_target = getattr(args, "managed_target", None)
        target = await asyncio.to_thread(
            publish_managed_run if managed_target is not None else publish_completed_run,
            output, script_file,
            managed_target if managed_target is not None else results_root,
        )
        print(f"  Published result: {target}", flush=True)
        return
    if getattr(args, "videos_only", False):
        output_root = args.output if args.output is not None else settings.output_dir
        await _run_videos_only(
            script_file, output_root, results_root=results_root,
            managed_target=getattr(args, "managed_target", None),
        )
        return
    if getattr(args, "images_only", False):
        output_root = args.output if args.output is not None else settings.output_dir
        await _run_images_only(
            script_file, output_root, no_video=bool(getattr(args, "no_video", False)),
            results_root=results_root,
            managed_target=getattr(args, "managed_target", None),
        )
        return
    if getattr(args, "regenerate_audio", False) or getattr(args, "resume_audio", False):
        output_root = args.output if args.output is not None else settings.output_dir
        await _run_audio_only(
            script_file,
            output_root,
            resume=bool(getattr(args, "resume_audio", False)),
            no_video=bool(getattr(args, "no_video", False)),
            results_root=results_root,
            managed_target=getattr(args, "managed_target", None),
        )
        return

    avatar_file = select_avatar(avatars_dir=args.avatars_dir, avatar_name=args.avatar)
    avatar_bytes, avatar_width, avatar_height = load_avatar_png(avatar_file)
    output_root_for_style = args.output if args.output is not None else settings.output_dir
    managed_target = getattr(args, "managed_target", None)
    if (
        managed_target is None
        and getattr(args, "from_stage", "B1.1") == "B2"
        and getattr(args, "visual_style", None) is None
    ):
        report_path = output_root_for_style / script_file.stem / "run_report.json"
        if report_path.is_file() and not report_path.is_symlink():
            previous_style = json.loads(
                report_path.read_text(encoding="utf-8")
            ).get("run", {}).get("visual_style", "casero")
            args.visual_style = previous_style
    avatar_file = _recover_avatar_layout(
        avatar_file, args.avatars_dir,
        reference_audio=getattr(args, "voice_audio", None),
    )
    if managed_target is not None:
        avatar_file = organize_avatar(
            avatar_file, args.avatars_dir,
            reference_audio=getattr(args, "voice_audio", None),
        )
        ensure_avatar_meta(
            avatar_file, args.avatars_dir,
            reference_audio=getattr(args, "voice_audio", None),
        )
        visual_style = ensure_avatar_style(
            avatar_file, getattr(args, "visual_style", None), select_visual_style
        )
    else:
        visual_style = select_visual_style(getattr(args, "visual_style", None))
    if getattr(args, "replace_professional_voice", False) and visual_style != "profesional":
        raise SystemExit("--replace-professional-voice is available only in Profesional")
    if visual_style != "casero" and getattr(args, "tts_provider", None) is not None:
        raise SystemExit("--tts-provider solo está disponible en modo casero.")
    if visual_style != "casero" and getattr(args, "ebook_cover", None):
        raise SystemExit("--ebook-cover solo se admite en modo casero.")
    covers = None
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing")
    if (
        args.from_stage == "B1.1"
        and getattr(args, "stop_stage", None) is None
        and not getattr(args, "no_audio", False)
    ):
        if visual_style == "casero" and not settings.ai33_api_key:
            raise SystemExit(
                "AI33_API_KEY is required for Casero TTS; pass --no-audio to skip."
            )
        if visual_style == "profesional" and not settings.gemini_api_key:
            raise SystemExit(
                "GEMINI_API_KEY is required for professional TTS; "
                "pass --no-audio to skip."
            )

    if args.max_parallel_calls < 1:
        raise SystemExit("--max-parallel-calls must be positive")
    # Preserve every source character: no strip(), newline normalization or rewriting.
    try:
        source_bytes = script_file.read_bytes()
        script = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"Script must be UTF-8: {script_file}") from exc
    if not script.strip():
        raise SystemExit(f"Script is empty: {script_file}")

    run_started = perf_counter()
    output_root = args.output if args.output is not None else settings.output_dir
    script_sha256 = hashlib.sha256(source_bytes).hexdigest()
    ledger = ApiCostLedger()
    timings = TimingLedger()
    resume_elapsed_offset = 0.0
    resume_expected_calls: dict[str, int | None] | None = None
    stop_stage = getattr(args, "stop_stage", None)
    stop_index = _STAGES.index(stop_stage) if stop_stage is not None else len(_STAGES) - 1
    planned_blocks: int | None = None
    previous_b11: B11Output | None = None
    previous_b12: tuple[B12Output, ...] | None = None
    previous_partial_b12: dict[int, B12Output] = {}
    previous_partial_b2: dict[int, B2Output | ProfessionalB2Output] = {}
    if args.from_stage == "B1.1":
        previous_output = output_root / script_file.stem
        if previous_output.exists() and has_pending_b2_video(previous_output):
            raise SystemExit(
                "Previous paid LTX jobs are incomplete. Use --videos-only to resume "
                "or reconcile their Postgres status before replacing this output."
            )
        qr = ensure_avatar_qr(
            avatar_file=avatar_file,
            avatars_dir=args.avatars_dir,
            input_url=getattr(args, "qr_url", None),
        )
        if visual_style == "profesional":
            if getattr(args, "tts_provider", None) is not None:
                raise SystemExit("Profesional uses only Gemini TTS; --tts-provider is Casero-only.")
            if getattr(args, "voice_audio", None) is not None:
                raise SystemExit("Profesional needs a Gemini voice ID, not --voice-audio.")
            avatar_file = organize_avatar(avatar_file, args.avatars_dir)
            voice = ensure_professional_avatar_voice(
                avatar_file=avatar_file,
                input_voice_id=getattr(args, "voice_id", None),
                use_default=bool(getattr(args, "default_voice", False)),
                allow_replace=bool(getattr(args, "replace_professional_voice", False)),
                gemini_api_key=settings.gemini_api_key,
            )
        else:
            voice = ensure_avatar_voice(
                avatar_file=avatar_file,
                avatars_dir=args.avatars_dir,
                input_voice_id=getattr(args, "voice_id", None),
                input_audio_filename=getattr(args, "voice_audio", None),
                use_default=bool(getattr(args, "default_voice", False)),
                default_voice_id=settings.ai33_fish_voice_id,
                api_key=settings.ai33_api_key,
                allow_sources=visual_style == "casero",
                input_provider=getattr(args, "tts_provider", None),
                activate=stop_stage is None and not getattr(args, "no_audio", False),
            )
            avatar_file = organize_avatar(
                avatar_file, args.avatars_dir, reference_audio=voice.audio_filename
            )
        if visual_style == "casero":
            ensure_avatar_meta(avatar_file, args.avatars_dir)
            data_root = args.avatars_dir if managed_target is not None else args.avatars_dir.parent
            covers = ensure_ebook_covers(
                avatar_file, data_dir=data_root, names=getattr(args, "ebook_cover", None),
            )
        output = _prepare_output(output_root, script_file, avatar_file=avatar_file)
    else:
        output = _prepare_resume_output(output_root, script_file, avatar_file=avatar_file)
        (previous_report, previous_b11, previous_b12,
         previous_partial_b12, previous_partial_b2) = _load_resume_checkpoint(
            output,
            start_from=args.from_stage,
            script=script,
            script_sha256=script_sha256,
        )
        if (args.from_stage == "B2" and
            previous_report["run"].get("visual_style", "casero") != visual_style):
            raise SystemExit(
                "B2 checkpoint style differs; choose the saved style or rerun from B1.2."
            )
        try:
            previous_costs = previous_report["api_costs"]
            previous_timing = previous_report["timings"]
            ledger.restore_requests(previous_costs["requests"])
            resume_elapsed_offset = timings.restore(previous_timing)
            prior_counts = {stage: timings.call_count(stage) for stage in _STAGES}
            rerun_from_index = _STAGES.index(args.from_stage)
            block_count = len(previous_b11.blocks or [])
            resume_expected_calls = {
                stage: prior_counts[stage]
                + (
                    (block_count - len(previous_partial_b12)
                     if stage == "B1.2" else
                     block_count - len(previous_partial_b2)
                     if stage == "B2" else block_count)
                    if rerun_from_index <= stage_index <= stop_index else 0
                )
                for stage_index, stage in enumerate(_STAGES)
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"Cannot restore prior run accounting: {exc}") from exc
        qr = ensure_avatar_qr(
            avatar_file=avatar_file,
            avatars_dir=args.avatars_dir,
            input_url=getattr(args, "qr_url", None),
        )
        if visual_style == "profesional":
            if getattr(args, "tts_provider", None) is not None:
                raise SystemExit("Profesional uses only Gemini TTS; --tts-provider is Casero-only.")
            if getattr(args, "voice_audio", None) is not None:
                raise SystemExit("Profesional needs a Gemini voice ID, not --voice-audio.")
            avatar_file = organize_avatar(avatar_file, args.avatars_dir)
            voice = ensure_professional_avatar_voice(
                avatar_file=avatar_file,
                input_voice_id=getattr(args, "voice_id", None),
                use_default=bool(getattr(args, "default_voice", False)),
                gemini_api_key=settings.gemini_api_key,
            )
        else:
            voice = ensure_avatar_voice(
                avatar_file=avatar_file,
                avatars_dir=args.avatars_dir,
                input_voice_id=getattr(args, "voice_id", None),
                input_audio_filename=getattr(args, "voice_audio", None),
                use_default=bool(getattr(args, "default_voice", False)),
                default_voice_id=settings.ai33_fish_voice_id,
                api_key=settings.ai33_api_key,
                allow_sources=visual_style == "casero",
                input_provider=getattr(args, "tts_provider", None),
                activate=False,
            )
            avatar_file = organize_avatar(
                avatar_file, args.avatars_dir, reference_audio=voice.audio_filename
            )
        _clear_outputs_from(output, args.from_stage)

    # Bind the voice to the run, not merely the mutable avatar library or .env.
    voice_snapshot = voice.snapshot()
    if args.from_stage != "B1.1":
        old_path = output / "avatar_voice.json"
        if old_path.is_symlink():
            raise SystemExit("El snapshot de voz del run no puede ser un enlace.")
        if old_path.is_file():
            try:
                saved_voice = json.loads(old_path.read_text(encoding="utf-8"))
                if not isinstance(saved_voice, dict):
                    raise ValueError("Voice snapshot is not an object")
                voice_snapshot = saved_voice
            except (OSError, ValueError) as exc:
                raise SystemExit(f"Snapshot de voz inválido: {old_path}") from exc
        else:
            old_avatar = previous_report.get("run", {}).get("avatar", {})
            if isinstance(old_avatar, dict) and isinstance(old_avatar.get("voice"), dict):
                voice_snapshot = old_avatar["voice"]
    _write(output / "avatar_voice.json", voice_snapshot)

    # Snapshot the selected PNG: later library changes cannot silently alter this run.
    (output / "avatar.png").write_bytes(avatar_bytes)
    (output / "qr.png").write_bytes(qr.png)
    avatar_metadata = {
        "filename": avatar_file.name,
        "source": avatar_file.as_posix(),
        "file": "avatar.png",
        "sha256": hashlib.sha256(avatar_bytes).hexdigest(),
        "width": avatar_width,
        "height": avatar_height,
        "voice": voice_snapshot,
        "qr": {
            "file": "qr.png",
            "url": qr.url,
            "sha256": qr.sha256,
            "generator": qr.generator,
            "verification": "deterministic_qr_encoding",
        },
    }
    run_metadata = {
        "script_file": script_file.name,
        "avatar": avatar_metadata,
        "script_sha256": script_sha256,
        "model": settings.openai_model,
        "reasoning_effort": settings.openai_reasoning_effort,
        "visual_style": visual_style,
    }
    if visual_style == "casero":
        run_metadata["casero_cta_enabled"] = True
    if stop_stage is not None:
        run_metadata["stopped_after"] = stop_stage
    if args.from_stage != "B1.1":
        old_publication = previous_report.get("run", {}).get("publication")
        if isinstance(old_publication, dict):
            run_metadata["publication"] = old_publication
        run_metadata["resumed_from"] = args.from_stage
        if previous_report.get("run", {}).get("audio_generation"):
            run_metadata["audio_generation"] = previous_report["run"]["audio_generation"]
    artifacts = BotArtifacts(output)

    def save_report(
        status: Literal["running", "completed", "failed"],
        blocks: int | None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        report_blocks = blocks if blocks is not None else planned_blocks
        if resume_expected_calls is None:
            expected_calls: dict[str, int | None] = {
                "B1.1": max(1, timings.call_count("B1.1")),
                "CTA": max(1, timings.call_count("CTA")) if visual_style == "casero" else 0,
                "CTA Ebook": timings.call_count("CTA Ebook"),
                "CTA Motion": timings.call_count("CTA Motion"),
                "CTA image": sum(r["stage"] == "CTA image" for r in ledger.records),
                "B1.2": (
                    max(report_blocks, timings.call_count("B1.2"))
                    if report_blocks is not None else None
                ) if stop_index >= 1 else 0,
                "B2": (
                    max(report_blocks, timings.call_count("B2"))
                    if report_blocks is not None else None
                ) if stop_index >= 2 else 0,
            }
        else:
            expected_calls = dict(resume_expected_calls)
            expected_calls["CTA"] = timings.call_count("CTA")
            expected_calls["CTA Ebook"] = timings.call_count("CTA Ebook")
            expected_calls["CTA Motion"] = timings.call_count("CTA Motion")
            expected_calls["CTA image"] = sum(
                r["stage"] == "CTA image" for r in ledger.records
            )
            if report_blocks is not None:
                expected_calls["B1.2"] = max(
                    expected_calls["B1.2"] or 0, timings.call_count("B1.2")
                )
                if stop_index >= 2:
                    expected_calls["B2"] = max(
                        expected_calls["B2"] or 0, report_blocks, timings.call_count("B2")
                    )
        ledger.sync_professional_voice_usage(output)
        ledger.sync_fish_director_usage(output)
        ledger.sync_image_usage(output)
        costs = ledger.report(
            blocks=report_blocks,
            run_status=status,
            expected_b11_calls=max(1, timings.call_count("B1.1")),
            expected_calls=expected_calls,
        )
        timing_report = timings.report(
            run_seconds=resume_elapsed_offset + perf_counter() - run_started,
            status=status,
        )
        salad = _attach_salad_costs(costs, output)
        report_payload = {
            "run": {**run_metadata, "status": status},
            "api_costs": costs,
            "timings": timing_report,
        }
        if salad is not None:
            report_payload["salad_costs"] = salad
        _write(output / "run_report.json", report_payload)
        return costs, timing_report

    def stage_complete(
        stage: str,
        blocks: int,
        merged_output: dict[str, object] | None,
    ) -> None:
        nonlocal planned_blocks
        planned_blocks = blocks
        if merged_output is not None:
            _write(output / stage / "merged_output.json", merged_output)
        costs, timing_report = save_report("running", blocks)
        elapsed = timing_report["stages"][stage]["elapsed_seconds"]
        if elapsed is None:
            raise RuntimeError(f"Missing elapsed time for completed stage {stage}")
        _print_metric(stage, elapsed, costs["stages"][stage]["estimated_cost_usd"])

    save_report("running", None)
    audio_task: asyncio.Task[dict[str, object]] | None = None
    cta_task: asyncio.Task[dict[str, object]] | None = None
    audio_joined = False
    image_manifest: dict[str, object] | None = None
    audio_enabled = (
        visual_style == "casero" and stop_stage is None and args.from_stage == "B1.1"
        and not getattr(args, "no_audio", False)
    )
    professional_audio_enabled = (
        visual_style == "profesional" and stop_stage is None
        and not getattr(args, "no_audio", False)
    )

    def output_and_start_audio(
        stage: str, block_id: int | None, payload: dict[str, object]
    ) -> None:
        nonlocal audio_task
        artifacts.output(stage, block_id, payload)
        if stage == "B1.1" and audio_enabled:
            # B1.1 is validated and materialized before on_output. Reuse exactly
            # its frozen block metadata; Fish and B1.2 now start together.
            b11 = B11Output.model_validate(payload)
            audio_task = asyncio.create_task(
                _generate_audio(
                    script,
                    output,
                    provider=provider,
                    b11=b11,
                    max_parallel_calls=args.max_parallel_calls,
                )
            )

            def report_audio_error(task: asyncio.Task[dict[str, object]]) -> None:
                if task.cancelled():
                    return
                error = task.exception()
                if error is not None:
                    print(f"Audio/STT failed: {error}", file=sys.stderr, flush=True)

            audio_task.add_done_callback(report_audio_error)

    try:
        provider = OpenAIProvider(
            api_key=settings.openai_api_key,
            reasoning_effort=settings.openai_reasoning_effort,
            service_tier="default",
        )
        if (
            visual_style == "casero" and stop_stage is None
            and not getattr(args, "no_audio", False)
            and not args.skip_images
        ):
            if args.from_stage != "B1.1":
                if not (output / "CTA" / "output.json").is_file():
                    raise RuntimeError(
                        "Saved casero run lacks CTA placement. Rerun planning from B1.1."
                    )
                if covers is None:
                    data_root = (
                        args.avatars_dir
                        if managed_target is not None else args.avatars_dir.parent
                    )
                    covers = ensure_ebook_covers(
                        avatar_file, data_dir=data_root, names=getattr(args, "ebook_cover", None),
                    )
            if covers is None:
                raise RuntimeError("Casero CTA covers were not prepared")
            qr_path = avatar_file.parent / "meta" / "qr.png"
            if not qr_path.is_file():
                qr_path = avatar_file.parent / "qr.png"
            if qr_path.is_symlink() or not qr_path.is_file() or (
                hashlib.sha256(qr_path.read_bytes()).hexdigest() != qr.sha256
            ):
                raise RuntimeError("The avatar CTA QR does not match its persisted PNG")
            cta_task = asyncio.create_task(prepare_casero_cta(
                avatar=avatar_file, covers=covers, qr_path=qr_path, qr_url=qr.url,
                script=script, voice_id=_run_voice_id(output),
                voice_provider=_run_voice_provider(output), provider=provider,
                model=settings.openai_model,
                on_api_response=ledger.record,
                on_call_duration=timings.record_call,
                on_image_usage=ledger.record_cta_image,
            ))
        if audio_enabled or professional_audio_enabled:
            run_metadata["audio_generation"] = {"status": "running"}
            save_report("running", None)
        elif args.from_stage == "B1.1":
            run_metadata["audio_generation"] = {"status": "skipped"}
            save_report("running", None)
        result = await run_b_pipeline(
            script,
            provider=provider,
            model=settings.openai_model,
            max_parallel_calls=args.max_parallel_calls,
            visual_style=visual_style,
            enable_casero_cta=visual_style == "casero",
            avatar_image=avatar_bytes,
            start_from=args.from_stage,
            stop_after=stop_stage or "B2",
            previous_b11=previous_b11,
            previous_b12=previous_b12,
            **(
                {"previous_partial_b12": previous_partial_b12}
                if previous_partial_b12 else {}
            ),
            **(
                {"previous_partial_b2": previous_partial_b2}
                if previous_partial_b2 else {}
            ),
            on_input=artifacts.input,
            on_output=output_and_start_audio,
            on_rejected_output=artifacts.rejected_b11,
            on_rejected_b12_output=artifacts.rejected_b12,
            on_rejected_b2_output=artifacts.rejected_b2,
            on_api_response=ledger.record,
            on_call_duration=timings.record_call,
            on_stage_duration=timings.record_stage,
            on_stage_complete=stage_complete,
        )
        if cta_task is not None:
            prepared_cta = await asyncio.shield(cta_task)
            run_metadata["cta_cache_dir"] = prepared_cta["directory"]
            run_metadata["casero_cta"] = {"status": prepared_cta["status"]}
            save_report("running", len(result.b12))
        elif visual_style == "casero" and args.from_stage != "B1.1":
            cache = avatar_file.parent / "meta" / "cta"
            if (cache / "identity.json").is_file() and (output / "CTA" / "output.json").is_file():
                run_metadata["cta_cache_dir"] = str(cache)
        visual_plan = None
        if stop_index >= 2:
            visual_plan = result.visual_plan()
            # This binding is intentionally absent from audited B2 payloads.
            visual_plan["avatar"] = avatar_metadata
            _write(output / "visual_plan.json", visual_plan)
        if professional_audio_enabled:
            if visual_plan is None:
                raise RuntimeError("Professional Gemini voice requires a completed B2 plan")
            if not settings.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY is required for professional Gemini 3.8 TTS")
            run_metadata["audio_generation"] = await generate_professional_audio(
                core=result.b11.narrative_core.model_dump(mode="json"),
                blocks=[item.blocks[0].model_dump(mode="json") for item in result.b2],
                output=output,
                provider=provider,
                model=settings.openai_model,
                gemini_api_key=settings.gemini_api_key,
                voice_id=_run_voice_id(output),
                source_sha256=script_sha256,
                on_response=ledger.record,
                on_timing=timings.record_call,
                on_stage_duration=timings.record_stage,
                max_parallel_calls=args.max_parallel_calls,
            )
            audio_joined = True
            voice_costs, voice_times = save_report("running", len(result.b12))
            for voice_stage in ("V1", "V2", "V3"):
                seconds = voice_times["stages"][voice_stage]["elapsed_seconds"]
                if seconds is not None:
                    _print_metric(
                        voice_stage, seconds,
                        voice_costs["stages"][voice_stage]["estimated_cost_usd"],
                    )
            if getattr(args, "managed_target", None) is not None:
                await asyncio.to_thread(sync_managed_outputs, output, args.managed_target)

        if audio_task is not None:
            if visual_plan is None:
                raise RuntimeError("Audio alignment requires a completed B2 plan")
            run_metadata["audio_generation"] = await audio_task
            audio_joined = True
            if getattr(args, "managed_target", None) is not None:
                save_report("running", len(result.b12))
                await asyncio.to_thread(
                    sync_managed_outputs, output, args.managed_target
                )

        if stop_stage is None and visual_plan is not None:
            # Fresh runs and --from B2 resumes go through the exact same recovery
            # path. Resumed B2 plans must be revalidated against saved Fish/STT
            # before any paid image requests are started.
            try:
                aligned = await _preflight_saved_run_audio(
                    output,
                    visual_plan,
                    run_metadata,
                )
            except Exception as exc:
                run_metadata["stt_alignment"] = {
                    "status": "failed",
                    "error": str(exc),
                }
                save_report("failed", len(result.b12))
                raise
            if aligned:
                save_report("running", len(result.b12))
        # Wait for valid audio/STT alignment before starting paid image work.
        if stop_stage is None and not getattr(args, "skip_images", False):
            if visual_plan is None:
                raise RuntimeError("Image generation requires a completed B2 plan")
            if args.from_stage == "B2":
                removed = await asyncio.to_thread(clear_b2_images_for_rerun, output)
                if removed:
                    print(
                        f"  Removed {removed} previous B2 image(s) before rerun.",
                        flush=True,
                    )
            run_metadata["image_generation"] = {"status": "running"}
            save_report("running", len(result.b12))
            image_started = perf_counter()
            # A downstream continuity failure is still a B2 validation error.
            # Spend the affected block's remaining B2 retries, never retry all blocks.
            while True:
                try:
                    image_manifest = await _generate_images(output, visual_plan)
                    break
                except ImageGenerationError as image_error:
                    retry_block_id = _continuity_retry_block_id(image_error)
                    if retry_block_id is None:
                        raise
                    if result.b2_correction_counts.get(retry_block_id, 0) >= 2:
                        raise RuntimeError(
                            f"B2 block {retry_block_id} exhausted its two validation "
                            "retries before image-grid continuity could be repaired"
                        ) from image_error
                    print(
                        f"  B2 bloque {retry_block_id}: numeración de continuidad inválida; "
                        "corrigiendo solo ese bloque...",
                        flush=True,
                    )
                    result = await run_b_pipeline(
                        script,
                        provider=provider,
                        model=settings.openai_model,
                        max_parallel_calls=args.max_parallel_calls,
                        visual_style=visual_style,
                        avatar_image=avatar_bytes,
                        start_from="B2",
                        stop_after="B2",
                        previous_b11=result.b11,
                        previous_b12=result.b12,
                        previous_b2=result.b2,
                        previous_b2_correction_counts=result.b2_correction_counts,
                        previous_b2_response_ids=result.b2_response_ids,
                        retry_b2_feedback={retry_block_id: str(image_error)},
                        retry_b2_block_ids=frozenset({retry_block_id}),
                        on_input=artifacts.input,
                        on_output=output_and_start_audio,
                        on_rejected_b2_output=artifacts.rejected_b2,
                        on_api_response=ledger.record,
                        on_call_duration=timings.record_call,
                        on_stage_duration=timings.record_stage,
                        on_stage_complete=stage_complete,
                    )
                    visual_plan = result.visual_plan()
                    visual_plan["avatar"] = avatar_metadata
                    _write(output / "visual_plan.json", visual_plan)
                    if professional_audio_enabled:
                        # This B2 block changed after professional V/audio ran.
                        # Re-direct from the corrected whole B2 plan. Identical
                        # paid Gemini fragments are reused by verified block SHA.
                        run_metadata["audio_generation"] = {"status": "running"}
                        save_report("running", len(result.b12))
                        run_metadata["audio_generation"] = await generate_professional_audio(
                            core=result.b11.narrative_core.model_dump(mode="json"),
                            blocks=[
                                item.blocks[0].model_dump(mode="json")
                                for item in result.b2
                            ],
                            output=output,
                            provider=provider,
                            model=settings.openai_model,
                            gemini_api_key=settings.gemini_api_key,
                            voice_id=_run_voice_id(output),
                            source_sha256=script_sha256,
                            on_response=ledger.record,
                            on_timing=timings.record_call,
                            on_stage_duration=timings.record_stage,
                            max_parallel_calls=args.max_parallel_calls,
                        )
                        await _preflight_saved_run_audio(
                            output, visual_plan, run_metadata,
                        )
                        save_report("running", len(result.b12))
            run_metadata["image_generation"] = _image_summary(image_manifest)
            image_costs, _ = save_report("running", len(result.b12))
            if getattr(args, "managed_target", None) is not None:
                await asyncio.to_thread(
                    sync_managed_outputs, output, args.managed_target
                )
            _print_metric(
                "B2 images",
                perf_counter() - image_started,
                image_costs["stages"]["B2 images"]["estimated_cost_usd"],
            )
        if image_manifest is not None:
            run_metadata["video_preview"] = {"status": "running"}
            save_report("running", len(result.b12))
            report = {"run": run_metadata}
            preview = await _preview_if_ready(
                output, report, plan=visual_plan, images=image_manifest
            )
            if preview is not None:
                run_metadata["video_preview"] = preview
                print(f"  Static video ready: {preview['file']}", flush=True)
                if getattr(args, "managed_target", None) is not None:
                    save_report("running", len(result.b12))
                    await asyncio.to_thread(
                        sync_managed_outputs, output, args.managed_target
                    )
            if getattr(args, "no_video", False):
                run_metadata["video_generation"] = {"status": "skipped"}
            else:
                run_metadata["video_generation"] = {"status": "running"}
                save_report("running", len(result.b12))
                video = await _video_if_ready(
                    output, {"run": run_metadata},
                    plan=visual_plan, images=image_manifest,
                )
                if video is not None:
                    run_metadata["video_generation"] = video
                    if video.get("status") == "completed":
                        print(f"  LTX video ready: {video['file']}", flush=True)
                else:
                    run_metadata["video_generation"] = {
                        "status": "deferred", "reason": "audio_or_images_incomplete",
                    }
    except Exception:
        if cta_task is not None and not cta_task.done():
            # Never abandon an in-flight paid CTA TTS or GPT Image POST.
            try:
                await asyncio.shield(cta_task)
            except Exception as exc:
                run_metadata["casero_cta"] = {"status": "incomplete", "error": str(exc)}
        if audio_task is not None and not audio_joined:
            # Never abandon an in-flight paid TTS submission by cancelling
            # its task when a separate B stage fails.
            try:
                run_metadata["audio_generation"] = await asyncio.shield(audio_task)
            except Exception:
                run_metadata["audio_generation"] = {"status": "incomplete"}
        if run_metadata.get("image_generation") == {"status": "running"}:
            run_metadata["image_generation"] = {"status": "incomplete"}
        if run_metadata.get("video_preview") == {"status": "running"}:
            run_metadata["video_preview"] = {"status": "incomplete"}
        if run_metadata.get("video_generation") == {"status": "running"}:
            run_metadata["video_generation"] = {"status": "incomplete"}
        costs, timing_report = save_report("failed", None)
        _print_metric("Run", timing_report["run_elapsed_seconds"], None)
        _print_observed_salad_total(costs)
        raise

    costs, timing_report = save_report("completed", len(result.b12))
    await asyncio.to_thread(
        _publish_if_ready, output, script_file, results_root, getattr(args, "managed_target", None)
    )
    _print_metric(
        "Run",
        timing_report["run_elapsed_seconds"],
        costs["estimated_total_usd"],
    )
    _print_observed_salad_total(costs)


if __name__ == "__main__":
    asyncio.run(main())
