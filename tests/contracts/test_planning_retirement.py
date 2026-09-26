"""Prevent accidental resurrection of the retired planning bots and runner."""

from pathlib import Path

ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)

RETIRED = (
    "src/ai_video_factory/legacy_bots",
    "src/ai_video_factory/workflows/production_runner.py",
    "scripts/pipeline/run_phase2.py",
    "scripts/pipeline/run_phase3.py",
    "scripts/pipeline/run_phase3_shots.py",
    "scripts/pipeline/run_phase4.py",
    "scripts/pipeline/run_phase5_beat_timing.py",
    "scripts/pipeline/run_phase6_storyboard.py",
    "scripts/pipeline/run_phase8_video_prompts.py",
    "scripts/pipeline/run_production.py",
    "scripts/pipeline/run_video_factory.ps1",
)


def test_retired_bot_paths_are_absent() -> None:
    assert all(not (ROOT / path).exists() for path in RETIRED)
    assert (ROOT / "src/ai_video_factory/bots/workflow.py").is_file()


def test_active_application_has_no_retired_bot_imports() -> None:
    for directory in ("src/ai_video_factory", "scripts"):
        for path in (ROOT / directory).rglob("*.py"):
            assert "legacy_bots" not in path.read_text(encoding="utf-8"), path


def test_independent_salad_control_plane_and_all_workers_are_preserved() -> None:
    for relative in (
        "deploy/salad/services.json",
        "src/ai_video_factory/inference/capacity_controller.py",
        "src/ai_video_factory/inference/repository.py",
        "src/ai_video_factory/providers/postgres_queue.py",
        "src/ai_video_factory/providers/inference_jobs.py",
        "src/ai_video_factory/providers/r2.py",
        "src/ai_video_factory/workers/breeze_tts2/runtime.py",
        "src/ai_video_factory/workers/fish_speech/runtime.py",
        "src/ai_video_factory/workers/whisper/runtime.py",
        "src/ai_video_factory/workers/ideogram4/runtime.py",
        "src/ai_video_factory/workers/qwen_image_21/runtime.py",
        "src/ai_video_factory/workers/ltx25/runtime.py",
        "src/ai_video_factory/workers/realesrgan/runtime.py",
        "scripts/salad/manage_salad_stack.ps1",
        "scripts/salad/run_salad_capacity_controller.py",
    ):
        assert (ROOT / relative).is_file(), relative
