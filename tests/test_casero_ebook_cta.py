"""Contract tests for casero-only reusable CTA asset selection and placement."""
from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.bots.cta import CTAPlacement, validate_placement
from ai_video_factory.providers.ebook_covers import ensure_ebook_covers
from ai_video_factory.workflows.casero_cta import split_cta_audio


def _png(path: Path, color: str) -> None:
    Image.new("RGB", (64, 96), color).save(path, format="PNG")


def test_covers_preserve_primary_order_and_survive_removed_originals(tmp_path: Path) -> None:
    data = tmp_path / "data"
    avatar = data / "Eva" / "Eva.png"
    avatar.parent.mkdir(parents=True)
    (avatar.parent / "meta").mkdir()
    _png(avatar, "white")
    _png(data / "A.png", "red")
    _png(data / "B.png", "blue")
    first = ensure_ebook_covers(
        avatar, data_dir=data, names=("A", "B"), interactive=False,
    )
    assert first.files == (
        avatar.parent / "meta" / "ebook" / "cover_1.png",
        avatar.parent / "meta" / "ebook" / "cover_2.png",
    )
    assert first.sha256 == tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in first.files)
    record = json.loads((avatar.parent / "meta" / "ebook_covers.json").read_text())
    assert [entry["name"] for entry in record["covers"]] == ["A", "B"]
    (data / "A.png").unlink()
    (data / "B.png").unlink()
    reused = ensure_ebook_covers(avatar, data_dir=data, names=None, interactive=False)
    assert reused.files == first.files
    assert reused.sha256 == first.sha256


@pytest.mark.parametrize("value", ["A.png", "../A", "", "A/B"])
def test_covers_accept_only_local_extensionless_stems(tmp_path: Path, value: str) -> None:
    root = tmp_path / "data"
    avatar = root / "Eva" / "Eva.png"
    avatar.parent.mkdir(parents=True)
    _png(avatar, "white")
    with pytest.raises(ValueError):
        ensure_ebook_covers(avatar, data_dir=root, names=[value], interactive=False)


def test_existing_cover_cannot_be_silently_changed(tmp_path: Path) -> None:
    root = tmp_path / "data"
    avatar = root / "Eva" / "Eva.png"
    avatar.parent.mkdir(parents=True)
    _png(avatar, "white")
    _png(root / "A.png", "red")
    ensure_ebook_covers(avatar, data_dir=root, names=["A"], interactive=False)
    _png(root / "A.png", "blue")
    with pytest.raises(ValueError, match="cambiado"):
        ensure_ebook_covers(avatar, data_dir=root, interactive=False)


def test_cta_placement_requires_unique_adjacent_original_sentences() -> None:
    script = "Intro hook. This is the full promise. Number one: do this. Next item."
    found = CTAPlacement(
        previous_sentence="This is the full promise.",
        next_sentence="Number one: do this.",
    )
    assert validate_placement(script, found) == script.index("Number one:") - 1
    with pytest.raises(ValueError, match="adjacent"):
        validate_placement(script, CTAPlacement(
            previous_sentence="Intro hook.", next_sentence="Number one: do this.",
        ))
    with pytest.raises(ValueError, match="exactly once"):
        validate_placement("Intro hook. Intro hook. Number one.", CTAPlacement(
            previous_sentence="Intro hook.", next_sentence="Number one.",
        ))


def test_cta_audio_first_half_is_exactly_half_of_even_pcm_samples(tmp_path: Path) -> None:
    master, first = tmp_path / "cta.wav", tmp_path / "first_half.wav"
    with wave.open(str(master), "wb") as out:
        out.setframerate(48000)
        out.setnchannels(1)
        out.setsampwidth(2)
        out.writeframes(b"\x00\x00" * 48001)
    duration = split_cta_audio(master, first)
    assert duration == 24001 / 48000
    with wave.open(str(master), "rb") as full, wave.open(str(first), "rb") as half:
        assert full.getnframes() == 48002
        assert half.getnframes() * 2 == full.getnframes()
        assert half.getframerate() == 48000
