import asyncio
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.pipeline import run_b_pipeline as runner


def _make_script(directory: Path, name: str, content: bytes = b"Guion.") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return path


def test_library_lists_only_txt_files_in_stable_name_order(tmp_path: Path) -> None:
    folder = tmp_path / "scripts"
    _make_script(folder, "zeta.txt")
    _make_script(folder, "Alfa.txt")
    _make_script(folder, "notes.md")
    _make_script(folder / "subfolder", "hidden.txt")

    assert [path.name for path in runner.available_scripts(folder)] == [
        "Alfa.txt",
        "zeta.txt",
    ]


def test_named_script_selects_existing_file_without_renaming(tmp_path: Path) -> None:
    folder = tmp_path / "scripts"
    first = _make_script(folder, "historia_roma.txt")
    _make_script(folder, "documental_japon.txt")

    assert runner.select_script(scripts_dir=folder, script_name="historia_roma") == first
    assert runner.select_script(scripts_dir=folder, script_name="historia_roma.txt") == first
    assert first.read_bytes() == b"Guion."


def test_unknown_named_script_does_not_fall_back_to_other_script(tmp_path: Path) -> None:
    folder = tmp_path / "scripts"
    _make_script(folder, "historia_roma.txt")

    with pytest.raises(SystemExit, match="not found"):
        runner.select_script(scripts_dir=folder, script_name="otro")


def test_interactive_menu_retries_until_a_valid_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "scripts"
    _make_script(folder, "a.txt")
    chosen = _make_script(folder, "b.txt")
    answers = iter(["0", "not a number", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert runner.select_script(scripts_dir=folder, interactive=True) == chosen
    output = capsys.readouterr().out
    assert "1. a.txt" in output and "2. b.txt" in output
    assert "Enter a number between 1 and 2" in output


def test_noninteractive_run_requires_explicit_script(tmp_path: Path) -> None:
    folder = tmp_path / "scripts"
    _make_script(folder, "a.txt")

    with pytest.raises(SystemExit, match="--script NAME.txt"):
        runner.select_script(scripts_dir=folder, interactive=False)


def test_missing_library_reports_where_to_put_scripts(tmp_path: Path) -> None:
    folder = tmp_path / "scripts"

    with pytest.raises(SystemExit, match="add one or more UTF-8"):
        runner.select_script(scripts_dir=folder, interactive=False)


def test_list_scripts_does_not_require_api_key_or_start_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "scripts"
    _make_script(folder, "roma.txt")
    args = Namespace(
        script_file=None,
        scripts_dir=folder,
        script=None,
        list_scripts=True,
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
) -> None:
    folder = tmp_path / "scripts"
    _make_script(folder, "roma.txt", b"  Uno.\r\n\r\nDos.  ")
    _make_script(folder, "japon.txt", b"Tres.\nCuatro.")
    args = Namespace(
        script_file=None,
        scripts_dir=folder,
        script="roma.txt",
        list_scripts=False,
        output=None,
        max_parallel_calls=3,
    )
    received: list[str] = []

    async def fake_run(script: str, *, provider: object, model: str, max_parallel_calls: int):
        assert provider.reasoning_effort == "medium"
        assert model == "gpt-6-luna"
        assert max_parallel_calls == 3
        received.append(script)
        return SimpleNamespace(
            b11=SimpleNamespace(model_dump=lambda: {"pipeline_stage": "B1.1"}),
            b12=[],
            b2=[],
            visual_plan=lambda: {"blocks": []},
        )

    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner.settings, "openai_api_key", "fake-key")
    monkeypatch.setattr(runner.settings, "openai_b_model", "gpt-6-luna")
    monkeypatch.setattr(runner.settings, "openai_b_reasoning_effort", "medium")
    monkeypatch.setattr(runner.settings, "output_dir", tmp_path / "output")
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(runner, "run_b_pipeline", fake_run)

    asyncio.run(runner.main())
    args.script = "japon.txt"
    asyncio.run(runner.main())

    assert received == ["  Uno.\r\n\r\nDos.  ", "Tres.\nCuatro."]
    assert (tmp_path / "output" / "b_pipeline" / "roma" / "b1_1.json").is_file()
    assert (tmp_path / "output" / "b_pipeline" / "japon" / "b1_1.json").is_file()
    assert (folder / "roma.txt").read_bytes() == b"  Uno.\r\n\r\nDos.  "


def test_existing_direct_path_still_supported(tmp_path: Path) -> None:
    direct = _make_script(tmp_path, "my_custom_name.txt")
    assert runner.select_script(
        scripts_dir=tmp_path / "missing", script_file=direct, interactive=False
    ) == direct
