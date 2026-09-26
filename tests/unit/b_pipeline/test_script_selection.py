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
    assert "No .txt scripts found in data/input" in capsys.readouterr().out


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


def test_selected_scripts_reach_b11_verbatim_and_outputs_do_not_collide(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "input"
    avatars = tmp_path / "avatar"
    monk = _make_avatar(avatars, "monje.png")
    teacher = _make_avatar(avatars, "maestra.png", color=(16, 48, 64))
    _make_script(folder, "roma.txt", b"  Uno.\r\n\r\nDos.  ")
    _make_script(folder, "japon.txt", b"Tres.\nCuatro.")
    args = Namespace(
        script_file=None,
        scripts_dir=folder,
        script="roma.txt",
        list_scripts=False,
        avatars_dir=avatars,
        avatar="monje.png",
        list_avatars=False,
        output=None,
        max_parallel_calls=3,
        from_stage="B1.1",
    )
    received: list[str] = []
    streamed_stages: list[str] = []
    printed_events: list[str] = []

    def observe_metric(name: str, seconds: float, cost: object) -> None:
        printed_events.append(name)
        original_metric(name, seconds, cost)

    original_metric = runner._print_metric

    async def fake_run(
        script: str,
        *,
        provider: object,
        model: str,
        max_parallel_calls: int,
        on_input,
        on_output,
        on_api_response,
        on_call_duration,
        on_stage_duration,
        on_stage_complete,
        on_rejected_output,
        start_from,
        previous_b11,
        previous_b12,
    ):
        assert start_from == "B1.1"
        assert previous_b11 is None and previous_b12 is None
        assert callable(on_rejected_output)
        assert provider.reasoning_effort == "medium"
        assert model == "gpt-6-luna"
        assert max_parallel_calls == 3
        received.append(script)
        call_start = len(streamed_stages)
        print_start = len(printed_events)
        on_input("B1.1", None, {"plain_script_for_recording": script})
        on_output("B1.1", None, {"pipeline_stage": "B1.1"})
        on_input("B1.2", 1, {"current_block_id": 1, "blocks": [{"text": script}]})
        on_output("B1.2", 1, {"pipeline_stage": "B1.2", "block_id": 1})
        on_input("B2", 1, {"current_block_id": 1, "blocks": [{"beats": []}]})
        on_output("B2", 1, {"pipeline_stage": "B2", "blocks": []})
        for stage, block_id in (("B1.1", None), ("B1.2", 1), ("B2", 1)):
            on_api_response(
                stage,
                block_id,
                SimpleNamespace(
                    id=f"resp_{stage}",
                    model="gpt-6-luna",
                    service_tier="default",
                    usage=SimpleNamespace(
                        input_tokens=100,
                        input_tokens_details=SimpleNamespace(
                            cached_tokens=0, cache_write_tokens=0
                        ),
                        output_tokens=20,
                        output_tokens_details=SimpleNamespace(reasoning_tokens=5),
                    ),
                ),
            )
            on_call_duration(stage, block_id, 0.125)
            on_stage_duration(stage, 0.250)
            merged = (
                None if stage == "B1.1"
                else {"pipeline_stage": stage, "blocks": [{"block_id": 1, "beats": []}]}
            )
            on_stage_complete(stage, 1, merged)
            streamed_stages.append(stage)
            completed_this_run = streamed_stages[call_start:]
            assert completed_this_run == ["B1.1", "B1.2", "B2"][:len(completed_this_run)]
            assert printed_events[print_start:] == completed_this_run
            report_path = tmp_path / "output" / ("roma" if "Uno." in script else "japon")
            partial = json.loads((report_path / "run_report.json").read_text(encoding="utf-8"))
            assert partial["run"]["status"] == "running"
            expected_avatar = "monje.png" if "Uno." in script else "maestra.png"
            assert partial["run"]["avatar"]["filename"] == expected_avatar
            assert partial["run"]["avatar"]["file"] == "avatar.png"
            assert (report_path / "avatar.png").is_file()
            assert partial["api_costs"]["stages"][stage]["estimated_cost_usd"] == (
                "0.00002000"
            )
            assert partial["timings"]["stages"][stage]["elapsed_seconds"] == 0.25
        return SimpleNamespace(
            b11=SimpleNamespace(model_dump=lambda: {"pipeline_stage": "B1.1"}),
            b12=[SimpleNamespace(block_id=1)],
            b2=[],
            visual_plan=lambda: {
                "blocks": [{
                    "beats": [
                        {"beat_id": "1A", "visual_type": "avatar", "description": None},
                        {"beat_id": "1B", "visual_type": "avatar_media", "description": "Mapa"},
                    ]
                }]
            },
            merged_b12_output=lambda: {
                "pipeline_stage": "B1.2",
                "blocks": [{"block_id": 1, "beats": []}],
            },
            merged_b2_output=lambda: {
                "pipeline_stage": "B2",
                "blocks": [{"block_id": 1, "beats": []}],
            },
        )

    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner.settings, "openai_api_key", "fake-key")
    monkeypatch.setattr(runner.settings, "openai_b_model", "gpt-6-luna")
    monkeypatch.setattr(runner.settings, "openai_b_reasoning_effort", "medium")
    monkeypatch.setattr(runner.settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(runner, "run_b_pipeline", fake_run)
    monkeypatch.setattr(runner, "_print_metric", observe_metric)

    async def fake_images(destination, plan):
        assert (destination / "B2" / "merged_output.json").is_file()
        assert plan["avatar"]["file"] == "avatar.png"
        return {
            "model_id": "gpt-image-2.5-flare",
            "items": [{"credit_cost": 882}],
        }

    monkeypatch.setattr(runner, "_generate_images", fake_images)

    asyncio.run(runner.main())
    previous_roma = tmp_path / "output" / "roma"
    (previous_roma / "stale_previous_run.json").write_text("obsolete", encoding="utf-8")
    args.script = "japon.txt"
    args.avatar = "maestra.png"
    asyncio.run(runner.main())
    args.script = "roma.txt"
    args.avatar = "monje.png"
    asyncio.run(runner.main())

    assert received == ["  Uno.\r\n\r\nDos.  ", "Tres.\nCuatro.", "  Uno.\r\n\r\nDos.  "]
    first = tmp_path / "output" / "roma"
    second = tmp_path / "output" / "japon"
    assert not (first / "stale_previous_run.json").exists()
    for destination in (first, second):
        assert (destination / "B1.1" / "input.json").is_file()
        assert (destination / "B1.1" / "output.json").is_file()
        assert (destination / "B1.2" / "block_1" / "input.json").is_file()
        assert (destination / "B1.2" / "block_1" / "output.json").is_file()
        assert (destination / "B2" / "block_1" / "input.json").is_file()
        assert (destination / "B2" / "block_1" / "output.json").is_file()
        b12_merged = json.loads(
            (destination / "B1.2" / "merged_output.json").read_text(encoding="utf-8")
        )
        b2_merged = json.loads(
            (destination / "B2" / "merged_output.json").read_text(encoding="utf-8")
        )
        assert [block["block_id"] for block in b12_merged["blocks"]] == [1]
        assert [block["block_id"] for block in b2_merged["blocks"]] == [1]
        consolidated = json.loads(
            (destination / "run_report.json").read_text(encoding="utf-8")
        )
        assert consolidated["run"]["status"] == "completed"
        assert consolidated["run"]["image_generation"] == {
            "status": "completed",
            "count": 1,
            "model_id": "gpt-image-2.5-flare",
            "credits": 882,
            "ai33_count": 1,
            "openai_fallback_count": 0,
            "ai33_timeout_charge_may_be_pending": False,
        }
        assert consolidated["run"]["script_file"] == destination.name + ".txt"
        expected_file = monk if destination == first else teacher
        avatar = consolidated["run"]["avatar"]
        assert avatar["filename"] == expected_file.name
        assert avatar["source"] == expected_file.as_posix()
        assert avatar["file"] == "avatar.png"
        assert avatar["sha256"] == hashlib.sha256(expected_file.read_bytes()).hexdigest()
        assert avatar["width"] == 2 and avatar["height"] == 2
        assert (destination / "avatar.png").read_bytes() == expected_file.read_bytes()
        plan = json.loads((destination / "visual_plan.json").read_text(encoding="utf-8"))
        assert plan["avatar"] == avatar
        assert [beat["visual_type"] for beat in plan["blocks"][0]["beats"]] == [
            "avatar", "avatar_media"
        ]
        assert "avatar" not in json.loads(
            (destination / "B2" / "block_1" / "output.json").read_text(encoding="utf-8")
        )
        assert consolidated["api_costs"]["pricing_status"] == "complete"
        assert consolidated["api_costs"]["estimated_total_usd"] == "0.00006000"
        timing = consolidated["timings"]
        assert timing["status"] == "completed"
        assert timing["run_elapsed_seconds"] >= 0
        assert timing["stages"]["B1.2"]["elapsed_seconds"] == 0.25
        assert timing["stages"]["B2"]["call_count"] == 1
        for old_name in ("api_costs.json", "timings.json", ".b_pipeline_run.json"):
            assert not (destination / old_name).exists()
    assert json.loads((first / "B1.1" / "input.json").read_text(encoding="utf-8")) == {
        "plain_script_for_recording": "  Uno.\r\n\r\nDos.  "
    }
    assert (folder / "roma.txt").read_bytes() == b"  Uno.\r\n\r\nDos.  "
    terminal = capsys.readouterr().out
    lines = terminal.splitlines()
    assert len(lines) == 12
    for offset in (0, 4, 8):
        assert lines[offset:offset + 3] == [
            "  B1.1 total: 0.250 s $0.00002000",
            "  B1.2 total: 0.250 s $0.00002000",
            "  B2 total: 0.250 s $0.00002000",
        ]
        assert re.fullmatch(
            r"  Run total: \d+\.\d{3} s \$0\.00006000",
            lines[offset + 3],
        ) is not None
    assert streamed_stages == ["B1.1", "B1.2", "B2"] * 3
    assert printed_events == ["B1.1", "B1.2", "B2", "Run"] * 3
    assert "This token-based estimate is not an OpenAI invoice" not in terminal


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
