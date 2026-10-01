"""Managed avatars keep their visual style fixed after the first selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_video_factory.providers.avatar_style import ensure_avatar_style


@pytest.fixture
def avatar(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "Jorge" / "Jorge.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not decoded by style selection")
    (path.parent / "meta").mkdir()
    return path


def test_first_explicit_style_is_saved_and_reused_without_prompt(avatar: Path) -> None:
    calls: list[str | None] = []

    def choose(selected: str | None) -> str:
        calls.append(selected)
        return selected or "casero"

    assert ensure_avatar_style(avatar, "profesional", choose) == "profesional"
    path = avatar.parent / "meta" / "style.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "avatar_filename": "Jorge.png",
        "visual_style": "profesional",
    }
    assert ensure_avatar_style(
        avatar, None, lambda _value: pytest.fail("Stored style must never prompt")
    ) == "profesional"
    assert ensure_avatar_style(
        avatar, "profesional", lambda _value: pytest.fail("Already saved")
    ) == "profesional"
    assert calls == ["profesional"]


def test_interactive_selection_is_saved_once(avatar: Path) -> None:
    inputs: list[str] = []

    def choose(selected: str | None) -> str:
        assert selected is None
        inputs.append("initial style prompt")
        return "casero"

    assert ensure_avatar_style(avatar, None, choose) == "casero"
    assert ensure_avatar_style(
        avatar, None, lambda _value: pytest.fail("Unexpected repeated style question")
    ) == "casero"
    assert len(inputs) == 1


def test_different_style_cannot_overwrite_avatar_metadata(avatar: Path) -> None:
    ensure_avatar_style(avatar, "casero", lambda selected: selected or "casero")
    record_path = avatar.parent / "meta" / "style.json"
    original = record_path.read_bytes()
    with pytest.raises(SystemExit, match="tiene fijado el estilo casero"):
        ensure_avatar_style(
            avatar, "profesional", lambda _value: pytest.fail("Should not prompt")
        )
    assert record_path.read_bytes() == original


def test_corrupt_or_wrong_avatar_record_fails_without_repicking(avatar: Path) -> None:
    record_path = avatar.parent / "meta" / "style.json"
    record_path.write_text('{"avatar_filename": "Other.png", "visual_style": "casero"}')
    with pytest.raises(SystemExit, match="Metadatos de estilo inválidos"):
        ensure_avatar_style(
            avatar, None, lambda _value: pytest.fail("Do not overwrite corrupt style")
        )


def test_missing_meta_is_not_silently_created_by_style_selector(
    avatar: Path,
) -> None:
    (avatar.parent / "meta").rmdir()
    with pytest.raises(SystemExit, match="Falta la carpeta meta"):
        ensure_avatar_style(avatar, "casero", lambda selected: selected or "casero")
