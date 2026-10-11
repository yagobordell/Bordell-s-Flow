import asyncio
import hashlib
import json
import re
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts.pipeline import run_b_pipeline as runner


def _make_script(directory: Path, name: str, content: bytes = b"Guion.") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return path


def _make_avatar(
    directory: Path, name: str, *, color: tuple[int, int, int] = (24, 48, 72)
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    Image.new("RGB", (2, 2), color).save(path, format="PNG")
    return path


@pytest.mark.parametrize("flag", ["--no-image", "--skip-images"])
def test_no_image_and_skip_images_are_equivalent_cli_flags(
    flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["run_b_pipeline.py", "--script", "roma.txt", "--avatar", "monje.png", flag],
    )
    args = runner.parse_args()
    assert args.skip_images is True
    assert args.images_only is False
    assert args.script == "roma.txt"
    assert args.avatar == "monje.png"


def test_no_image_cannot_be_combined_with_images_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["run_b_pipeline.py", "--images-only", "--no-image"],
    )
    with pytest.raises(SystemExit):
        runner.parse_args()


@pytest.mark.parametrize(
    ("flags", "start", "stop"),
    [
        (["--stop", "B1.1"], "B1.1", "B1.1"),
        (["--from", "B1.2", "--stop", "B1.2"], "B1.2", "B1.2"),
        (["--from", "B2", "--stop", "B2"], "B2", "B2"),
    ],
)
def test_stop_cli_accepts_inclusive_stage_range(
    flags: list[str], start: str, stop: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["run_b_pipeline.py", *flags])
    args = runner.parse_args()
    assert args.from_stage == start
    assert args.stop_stage == stop


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--from", "B2", "--stop", "B1.2"], "--stop must be"),
        (["--images-only", "--stop", "B2"], "--images-only cannot"),
        (["--regenerate-audio", "--stop", "B1.1"], "Audio-only modes cannot"),
        (["--list-scripts", "--stop", "B1.1"], "Listing cannot"),
    ],
)
def test_stop_cli_rejects_invalid_or_conflicting_modes(
    flags: list[str], message: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["run_b_pipeline.py", *flags])
    with pytest.raises(SystemExit) as raised:
        runner.parse_args()
    assert raised.value.code == 2
    assert message in capsys.readouterr().err


def test_professional_audio_resume_before_gemini_task_is_safe(tmp_path: Path) -> None:
    nonce = runner._professional_audio_nonce(
        [], resume=True, previous_audio={"status": "incomplete"},
    )
    assert isinstance(nonce, str) and len(nonce) == 32

    # Reuse the immutable saved paid task identity when Gemini is pending.
    pending = tmp_path / "gemini-existing" / "task.json"
    pending.parent.mkdir(parents=True)
    pending.write_text(json.dumps({"generation_nonce": "paid-task"}), encoding="utf-8")
    assert runner._professional_audio_nonce(
        [pending], resume=True, previous_audio={"status": "incomplete"},
    ) == "paid-task"

    with pytest.raises(RuntimeError, match="multiple pending Gemini"):
        runner._professional_audio_nonce(
            [pending, pending], resume=True, previous_audio={"status": "incomplete"},
        )
    with pytest.raises(RuntimeError, match="incomplete paid audio task"):
        runner._professional_audio_nonce(
            [pending], resume=False, previous_audio={"status": "incomplete"},
        )
    with pytest.raises(RuntimeError, match="completed professional audio"):
        runner._professional_audio_nonce(
            [], resume=True, previous_audio={"status": "completed"},
        )


def test_default_scripts_location_is_input_root() -> None:
    assert runner.DEFAULT_SCRIPTS_DIR == Path("data/input")


def test_missing_default_input_root_is_created_without_a_gitkeep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    args = Namespace(
        script_file=None,
        scripts_dir=runner.DEFAULT_SCRIPTS_DIR,
        script=None,
        list_scripts=True,
        avatars_dir=tmp_path / "avatar",
        avatar=None,
        list_avatars=False,
        output=None,
        max_parallel_calls=8,
    )
    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner.settings, "openai_api_key", None)
    monkeypatch.setattr(
        runner, "OpenAIProvider",
        lambda **_kwargs: pytest.fail("Listing must not create a provider"),
    )

    asyncio.run(runner.main())

    assert (tmp_path / "data" / "input").is_dir()
    assert not (tmp_path / "data" / "input" / "scripts").exists()
    output = capsys.readouterr().out.replace("\\", "/")
    assert "No .txt scripts found in data/input" in output


def test_library_lists_only_txt_files_in_stable_name_order(tmp_path: Path) -> None:
    folder = tmp_path / "input"
    _make_script(folder, "zeta.txt")
    _make_script(folder, "Alfa.txt")
    _make_script(folder, "notes.md")
    _make_script(folder / "subfolder", "hidden.txt")

    assert [path.name for path in runner.available_scripts(folder)] == [
        "Alfa.txt",
        "zeta.txt",
    ]


def test_named_script_selects_existing_file_without_renaming(tmp_path: Path) -> None:
    folder = tmp_path / "input"
    first = _make_script(folder, "historia_roma.txt")
    _make_script(folder, "documental_japon.txt")

    assert runner.select_script(scripts_dir=folder, script_name="historia_roma") == first
    assert runner.select_script(scripts_dir=folder, script_name="historia_roma.txt") == first
    assert first.read_bytes() == b"Guion."


def test_unknown_named_script_does_not_fall_back_to_other_script(tmp_path: Path) -> None:
    folder = tmp_path / "input"
    _make_script(folder, "historia_roma.txt")

    with pytest.raises(SystemExit, match="not found"):
        runner.select_script(scripts_dir=folder, script_name="otro")


def test_interactive_menu_retries_until_a_valid_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "input"
    _make_script(folder, "a.txt")
    chosen = _make_script(folder, "b.txt")
    answers = iter(["0", "not a number", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert runner.select_script(scripts_dir=folder, interactive=True) == chosen
    output = capsys.readouterr().out
    assert "1. a.txt" in output and "2. b.txt" in output
    assert "Enter a number between 1 and 2" in output


def test_noninteractive_run_requires_explicit_script(tmp_path: Path) -> None:
    folder = tmp_path / "input"
    _make_script(folder, "a.txt")

    with pytest.raises(SystemExit, match="--script NAME.txt"):
        runner.select_script(scripts_dir=folder, interactive=False)


def test_missing_library_reports_where_to_put_scripts(tmp_path: Path) -> None:
    folder = tmp_path / "input"

    with pytest.raises(SystemExit, match="add one or more UTF-8"):
        runner.select_script(scripts_dir=folder, interactive=False)


def test_list_scripts_does_not_require_api_key_or_start_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "input"
    _make_script(folder, "roma.txt")
    args = Namespace(
        script_file=None,
        scripts_dir=folder,
        script=None,
        list_scripts=True,
        avatars_dir=tmp_path / "avatar",
        avatar=None,
        list_avatars=False,
        output=None,
        max_parallel_calls=8,
    )
    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner.settings, "openai_api_key", None)
    monkeypatch.setattr(
        runner,
        "OpenAIProvider",
        lambda **_kwargs: pytest.fail("Listing must not create a provider"),
    )

    asyncio.run(runner.main())
    assert "roma.txt" in capsys.readouterr().out


def test_selected_scripts_reach_professional_director_and_outputs_do_not_collide(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Whole scripts reach one Professional director; stop saves isolated checkpoints."""
    from ai_video_factory.agents.professional import runner as director_module
    from ai_video_factory.bots.contracts import B11Output

    folder = tmp_path / "input"
    avatars = tmp_path / "avatar"
    monk = _make_avatar(avatars, "monje.png")
    teacher = _make_avatar(avatars, "maestra.png", color=(16, 48, 64))
    _make_script(folder, "roma.txt", b"  Uno.\r\n\r\nDos.  ")
    _make_script(folder, "japon.txt", b"Tres.\nCuatro.")
    args = Namespace(
        visual_style="profesional", script_file=None, scripts_dir=folder,
        script="roma.txt", list_scripts=False, avatars_dir=avatars,
        avatar="monje.png", list_avatars=False, output=tmp_path / "output",
        max_parallel_calls=3, from_stage="B1.1", stop_stage="openai-agent",
        no_audio=True, skip_images=False, images_only=False,
        regenerate_audio=False, resume_audio=False, voice_id="Kore",
    )
    received: list[str] = []
    original_scripts = ["  Uno.\r\n\r\nDos.  ", "Tres.\nCuatro."]
    qr_png = _make_avatar(tmp_path, "fake_qr.png")

    async def fake_director(source, **_kwargs):
        received.append(source)
        marker = "Dos." if "Uno." in source else "Cuatro."
        boundary = source.index(marker)
        text_parts = [source[:boundary], source[boundary:]]
        core = {"central_question": "Which idea?", "final_answer": "The second."}
        group = {
            "block_id": 1, "type": "development", "emotional_entry": "curious",
            "emotional_exit": "clear",
            "span": {"first_words": source.strip().split()[0], "last_words": marker},
        }
        b11 = B11Output.model_validate({
            "pipeline_stage": "B1.1", "narrative_core": core,
            "blocks": [group], "error": None,
        })
        beats = [{
            "beat_id": f"1{letter}", "text": fragment,
            "beat_type": "claim", "visual_type": "media_image",
            "prompt": f"One useful photograph {letter}",
            "video_prompt": None, "audio": False, "reference": False,
        } for letter, fragment in zip("AB", text_parts, strict=True)]
        block = {**group, "beats": beats}
        return {
            "b11": b11,
            "projection": {
                "b2_merged": {"blocks": [block]},
                "visual_plan": {
                    "visual_style": "profesional", "narrative_core": core,
                    "blocks": [block], "ebook_cta_groups": [],
                },
                "editor_plan": {"schema_version": "professional-editor-notes-v1",
                                "blocks": []},
            },
            "usage": [
                {"phase": f"0{i}_{name}", "input_tokens": 100,
                 "output_tokens": 25, "total_tokens": 125}
                for i, name in enumerate(
                    ["groups", "planning", "evaluation", "correction", "delivery"], 1
                )
            ],
            "validation": {"summary": {"avatar_percentage": 0.0}},
        }

    def fake_avatar_qr(**_kwargs):
        data = qr_png.read_bytes()
        return SimpleNamespace(
            url="https://example.com/ebook",
            png=data,
            sha256=hashlib.sha256(data).hexdigest(),
            generator="segno",
        )

    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner.settings, "openai_api_key", "fake-key")
    monkeypatch.setattr(runner.settings, "openai_model", "gpt-6-luna")
    monkeypatch.setattr(runner.settings, "openai_reasoning_effort", "medium")
    monkeypatch.setattr(runner.settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(runner, "ensure_avatar_qr", fake_avatar_qr)
    monkeypatch.setattr(director_module, "run_professional_director", fake_director)
    monkeypatch.setattr(
        runner, "_generate_images",
        lambda *_args, **_kwargs: pytest.fail("Agent stop must not generate images"),
    )
    monkeypatch.setattr(
        runner, "generate_professional_audio",
        lambda *_args, **_kwargs: pytest.fail("Agent stop must not generate voice"),
    )

    asyncio.run(runner.main())
    first = tmp_path / "output" / "roma"
    (first / "stale_previous_run.json").write_text("obsolete", encoding="utf-8")
    args.script, args.avatar = "japon.txt", "maestra.png"
    asyncio.run(runner.main())
    args.script, args.avatar = "roma.txt", "monje.png"
    asyncio.run(runner.main())

    assert received == [original_scripts[0], original_scripts[1], original_scripts[0]]
    second = tmp_path / "output" / "japon"
    assert not (first / "stale_previous_run.json").exists()
    for destination in (first, second):
        assert (destination / "B1.1" / "output.json").is_file()
        assert (destination / "B1.2" / "block_1" / "output.json").is_file()
        assert (destination / "B2" / "block_1" / "output.json").is_file()
        assert (destination / "B2" / "merged_output.json").is_file()
        assert (destination / "visual_plan.json").is_file()
        assert (destination / "agent_openai" / "editor_plan.json").is_file()
        assert not (destination / "B3").exists()
        assert not (destination / "audio").exists()
        assert not (destination / "images").exists()
        report = json.loads((destination / "run_report.json").read_text(encoding="utf-8"))
        assert report["run"]["stopped_after"] == "openai-agent"
        assert report["run"]["openai_agent"]["status"] == "completed"
        assert report["run"]["openai_agent"]["avatar_percentage"] == 0.0
        assert report["run"]["status"] == "completed"
        original_file = monk if destination == first else teacher
        expected_file = original_file.parent / original_file.stem / original_file.name
        assert expected_file.is_file()
        plan = json.loads((destination / "visual_plan.json").read_text(encoding="utf-8"))
        source = original_scripts[0] if destination == first else original_scripts[1]
        assert "".join(
            beat["text"] for block in plan["blocks"] for beat in block["beats"]
        ) == source
        assert plan["avatar"]["file"] == "avatar.png"
        assert report["run"]["avatar"]["sha256"] == hashlib.sha256(
            expected_file.read_bytes()
        ).hexdigest()
    assert "Cinco fases completadas" in capsys.readouterr().out


def test_existing_direct_path_still_supported(tmp_path: Path) -> None:
    direct = _make_script(tmp_path, "my_custom_name.txt")
    assert runner.select_script(
        scripts_dir=tmp_path / "missing", script_file=direct, interactive=False
    ) == direct


def test_repeating_same_script_resets_only_that_scripts_output(tmp_path: Path) -> None:
    library = tmp_path / "input"
    roma = _make_script(library, "roma.txt")
    _make_script(library, "japon.txt")
    root = tmp_path / "output"
    old = runner._prepare_output(root, roma)
    runner._write(old / "run_report.json", {"run": {"script_file": "roma.txt"}})
    (old / "stale.json").write_text("stale", encoding="utf-8")
    other = root / "japon"
    other.mkdir()
    (other / "keep.json").write_text("other-script", encoding="utf-8")

    replacement = runner._prepare_output(root, roma)

    assert replacement == old
    assert not (replacement / "stale.json").exists()
    assert (other / "keep.json").read_text(encoding="utf-8") == "other-script"
    assert roma.is_file()


def test_reset_rejects_unrelated_existing_directories_and_input_roots(
    tmp_path: Path,
) -> None:
    script = _make_script(tmp_path / "input", "roma.txt")
    output_root = tmp_path / "output"
    unrelated = output_root / "roma"
    unrelated.mkdir(parents=True)
    (unrelated / "notes.txt").write_text("do not delete", encoding="utf-8")

    with pytest.raises(SystemExit, match="unrelated files"):
        runner._prepare_output(output_root, script)
    assert (unrelated / "notes.txt").read_text(encoding="utf-8") == "do not delete"

    with pytest.raises(SystemExit, match="input directory"):
        runner._prepare_output(script.parent, script)


def test_reset_recognizes_previous_standalone_runner_output(tmp_path: Path) -> None:
    script = _make_script(tmp_path / "input", "old.txt")
    old = tmp_path / "output" / "old"
    old.mkdir(parents=True)
    (old / "b1_1.json").write_text("old", encoding="utf-8")

    replacement = runner._prepare_output(tmp_path / "output", script)

    assert replacement == old
    assert list(replacement.iterdir()) == []


def test_stage_wall_clock_is_not_sum_of_parallel_call_durations() -> None:
    ledger = runner.TimingLedger()
    ledger.record_call("B1.2", 2, 12.0)
    ledger.record_call("B1.2", 1, 10.0)
    ledger.record_stage("B1.2", 12.5)
    ledger.record_stage("B1.1", 3.2)
    ledger.record_call("B1.1", None, 3.0)

    result = ledger.report(run_seconds=20.6, status="completed")

    assert result["run_elapsed_seconds"] == 20.6
    assert result["stages"]["B1.2"]["elapsed_seconds"] == 12.5
    assert result["stages"]["B1.2"]["sum_call_seconds"] == 22.0
    assert result["stages"]["B2"]["elapsed_seconds"] is None
    assert result["calls"][1]["block_id"] == 1
    assert result["calls"][2]["block_id"] == 2


def test_single_text_model_and_medium_reasoning_configuration() -> None:
    from ai_video_factory.config import Settings

    fields = Settings.model_fields
    assert fields["openai_model"].default == "gpt-6-luna"
    assert fields["openai_reasoning_effort"].default == "medium"
    assert "openai_b_model" not in fields
    assert "openai_b_reasoning_effort" not in fields


