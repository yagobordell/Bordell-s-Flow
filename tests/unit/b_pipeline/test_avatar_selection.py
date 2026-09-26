"""Avatar library, safe preflight and persistent run-level visual binding."""

import asyncio
import json
from argparse import Namespace
from pathlib import Path

import pytest
from PIL import Image

from scripts.pipeline import run_b_pipeline as runner


def _avatar(folder: Path, name: str, *, color: tuple[int, int, int] = (20, 30, 40)) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    Image.new("RGB", (3, 2), color).save(path, format="PNG")
    return path


def test_default_avatar_library_is_data_avatar() -> None:
    assert runner.DEFAULT_AVATARS_DIR == Path("data/avatar")


def test_avatar_library_lists_png_files_only_and_sorts_names(tmp_path: Path) -> None:
    folder = tmp_path / "avatar"
    _avatar(folder, "zeta.png")
    _avatar(folder, "Alfa.PNG")
    _avatar(folder / "nested", "hidden.png")
    (folder / "avatar.jpg").write_bytes(b"not an avatar")

    assert [file.name for file in runner.available_avatars(folder)] == [
        "Alfa.PNG", "zeta.png"
    ]


def test_select_avatar_by_exact_name_or_png_suffix(tmp_path: Path) -> None:
    folder = tmp_path / "avatar"
    monk = _avatar(folder, "monje.png")
    _avatar(folder, "otro.png")

    assert runner.select_avatar(avatars_dir=folder, avatar_name="monje") == monk
    assert runner.select_avatar(avatars_dir=folder, avatar_name="monje.png") == monk
    with pytest.raises(SystemExit, match="not found"):
        runner.select_avatar(avatars_dir=folder, avatar_name="../monje.png")
    with pytest.raises(SystemExit, match="not found"):
        runner.select_avatar(avatars_dir=folder, avatar_name="no-existe")


def test_avatar_menu_retries_and_selects_exact_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "avatar"
    _avatar(folder, "a.png")
    chosen = _avatar(folder, "b.png")
    answers = iter(["0", "otro", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert runner.select_avatar(avatars_dir=folder, interactive=True) == chosen
    output = capsys.readouterr().out
    assert "1. a.png" in output and "2. b.png" in output
    assert "Enter a number between 1 and 2" in output


def test_avatar_menu_cancellation_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "avatar"
    _avatar(folder, "monje.png")
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    with pytest.raises(SystemExit, match="Avatar selection cancelled"):
        runner.select_avatar(avatars_dir=folder, interactive=True)


def test_noninteractive_avatar_selection_fails_closed(tmp_path: Path) -> None:
    folder = tmp_path / "avatar"
    _avatar(folder, "monje.png")
    with pytest.raises(SystemExit, match="--avatar NAME.png"):
        runner.select_avatar(avatars_dir=folder, interactive=False)


def test_missing_avatar_library_does_not_select_default(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="Add one or more valid"):
        runner.select_avatar(avatars_dir=tmp_path / "missing", interactive=False)


def test_avatar_png_is_verified_and_original_bytes_are_preserved(tmp_path: Path) -> None:
    folder = tmp_path / "avatar"
    good = _avatar(folder, "monje.png")
    content, width, height = runner.load_avatar_png(good)

    assert content == good.read_bytes()
    assert (width, height) == (3, 2)

    broken = folder / "falso.png"
    broken.write_bytes(b"no es un PNG")
    with pytest.raises(SystemExit, match="Invalid PNG avatar"):
        runner.load_avatar_png(broken)

    jpeg = folder / "jpeg.png"
    Image.new("RGB", (3, 2), (1, 2, 3)).save(jpeg, format="JPEG")
    with pytest.raises(SystemExit, match="Invalid PNG avatar"):
        runner.load_avatar_png(jpeg)


def test_output_root_cannot_contain_selected_avatar(tmp_path: Path) -> None:
    folder = tmp_path / "avatar"
    image = _avatar(folder, "monje.png")
    script = tmp_path / "input" / "roma.txt"
    script.parent.mkdir()
    script.write_text("Guion", encoding="utf-8")

    with pytest.raises(SystemExit, match="input directory"):
        runner._prepare_output(folder, script, avatar_file=image)
    with pytest.raises(SystemExit, match="input directory"):
        runner._prepare_output(folder / "outputs", script, avatar_file=image)
    assert image.is_file()


def test_list_avatars_creates_library_without_key_or_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    args = Namespace(
        script_file=None,
        scripts_dir=runner.DEFAULT_SCRIPTS_DIR,
        script=None,
        list_scripts=False,
        avatars_dir=runner.DEFAULT_AVATARS_DIR,
        avatar=None,
        list_avatars=True,
        output=None,
        max_parallel_calls=8,
    )
    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner.settings, "openai_api_key", None)
    monkeypatch.setattr(
        runner, "OpenAIProvider",
        lambda **_kwargs: pytest.fail("Listing must not instantiate the provider"),
    )
    asyncio.run(runner.main())

    assert (tmp_path / "data" / "avatar").is_dir()
    assert "No PNG avatars found in data/avatar" in capsys.readouterr().out

    _avatar(tmp_path / "data" / "avatar", "monje.png")
    asyncio.run(runner.main())
    assert "monje.png" in capsys.readouterr().out


def test_missing_or_invalid_avatar_preserves_previous_run_before_api_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = tmp_path / "input"
    scripts.mkdir()
    (scripts / "roma.txt").write_text("Guion.", encoding="utf-8")
    avatars = tmp_path / "avatar"
    avatars.mkdir()
    output = tmp_path / "output"
    existing = output / "roma"
    existing.mkdir(parents=True)
    (existing / "run_report.json").write_text('{"previous": true}', encoding="utf-8")
    (existing / "keep.txt").write_text("previous output", encoding="utf-8")
    args = Namespace(
        script_file=None,
        scripts_dir=scripts,
        script="roma.txt",
        list_scripts=False,
        avatars_dir=avatars,
        avatar="missing.png",
        list_avatars=False,
        output=output,
        max_parallel_calls=8,
    )
    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner.settings, "openai_api_key", "fake-key")
    monkeypatch.setattr(
        runner, "OpenAIProvider",
        lambda **_kwargs: pytest.fail("Invalid avatar must not trigger an API call"),
    )

    with pytest.raises(SystemExit, match="No PNG avatars found"):
        asyncio.run(runner.main())
    invalid = avatars / "broken.png"
    invalid.write_bytes(b"not png")
    args.avatar = "broken.png"
    with pytest.raises(SystemExit, match="Invalid PNG avatar"):
        asyncio.run(runner.main())
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "previous output"
    assert json.loads((existing / "run_report.json").read_text(encoding="utf-8")) == {
        "previous": True
    }
