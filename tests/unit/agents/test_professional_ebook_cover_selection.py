"""No-API regression: Professional CTA cover is prompted, saved and reused."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.providers.ebook_covers import resolve_professional_ebook_covers


SPOKEN_CTA = (
    "Here is my ebook explaining these warning signs. "
    "Scan the QR code to download the ebook."
)
OTHER_SCRIPT = (
    "A person receives a call and checks who is calling "
    "before deciding whether to answer."
)


def _assets(root: Path) -> tuple[Path, Path]:
    data = root / "data"
    data.mkdir()
    avatar = data / "Enzo" / "Enzo.png"
    avatar.parent.mkdir()
    Image.new("RGB", (12, 12), "white").save(avatar, format="PNG")
    Image.new("RGB", (18, 24), "blue").save(data / "real-ebook.png", format="PNG")
    return data, avatar


def test_first_professional_ebook_cta_asks_for_png_and_remembers_it(
    tmp_path, monkeypatch,
):
    data, avatar = _assets(tmp_path)
    prompts = []
    def choose(prompt: str) -> str:
        prompts.append(prompt)
        return "real-ebook.png"

    monkeypatch.setattr("builtins.input", choose)
    covers = resolve_professional_ebook_covers(
        avatar, script=SPOKEN_CTA, data_dir=data, interactive=True,
    )
    assert covers is not None
    assert len(prompts) == 1
    assert covers.files[0] == avatar.parent / "meta" / "ebook" / "cover_1.png"
    assert covers.files[0].is_file()
    saved = json.loads(
        (avatar.parent / "meta" / "ebook_covers.json").read_text(encoding="utf-8")
    )
    assert saved["covers"][0]["name"] == "real-ebook"

    # No second question, even unattended or on --rerun, when the cover exists.
    def forbidden(_prompt: str) -> str:
        raise AssertionError("The saved cover must not be prompted again")

    monkeypatch.setattr("builtins.input", forbidden)
    repeated = resolve_professional_ebook_covers(
        avatar, script=SPOKEN_CTA, data_dir=data, interactive=True,
    )
    unattended = resolve_professional_ebook_covers(
        avatar, script=SPOKEN_CTA, data_dir=data, interactive=False,
    )
    assert repeated == covers == unattended


def test_wrong_cover_name_reasks_instead_of_raising_traceback(
    tmp_path, monkeypatch, capsys,
):
    data, avatar = _assets(tmp_path)
    sequence = iter(["not-present.png", "../invalid.png", "real-ebook"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(sequence))
    covers = resolve_professional_ebook_covers(
        avatar, script=SPOKEN_CTA, data_dir=data, interactive=True,
    )
    assert covers is not None
    assert "Portada no encontrada" in capsys.readouterr().out


def test_noninteractive_missing_cover_requests_one_explicit_flag(tmp_path):
    data, avatar = _assets(tmp_path)
    with pytest.raises(ValueError, match="--ebook-cover NOMBRE"):
        resolve_professional_ebook_covers(
            avatar, script=SPOKEN_CTA, data_dir=data, interactive=False,
        )
    assert not (avatar.parent / "meta" / "ebook_covers.json").exists()


def test_explicit_cover_name_is_supported_without_terminal(tmp_path):
    data, avatar = _assets(tmp_path)
    covers = resolve_professional_ebook_covers(
        avatar, script=SPOKEN_CTA, data_dir=data,
        names=["real-ebook"], interactive=False,
    )
    assert covers is not None
    assert covers.files[0].is_file()


def test_no_cover_is_requested_for_non_promotional_script(tmp_path, monkeypatch):
    data, avatar = _assets(tmp_path)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("Non-promotional scripts should not prompt"),
    )
    assert resolve_professional_ebook_covers(
        avatar, script=OTHER_SCRIPT, data_dir=data, interactive=True,
    ) is None
    assert not (avatar.parent / "meta" / "ebook_covers.json").exists()


def test_selection_is_cancelable_without_changing_avatar_configuration(
    tmp_path, monkeypatch,
):
    data, avatar = _assets(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    with pytest.raises(ValueError, match="Selección de portada cancelada"):
        resolve_professional_ebook_covers(
            avatar, script=SPOKEN_CTA, data_dir=data, interactive=True,
        )
    assert not (avatar.parent / "meta" / "ebook_covers.json").exists()
