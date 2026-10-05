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
from ai_video_factory.workers.ltx25.a2v import (
    LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE,
    LTX_A2V_RUN_OPTIMIZED_PROFILE,
    LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE,
    LTX_A2V_RUN_VAE_BACKEND,
)
from ai_video_factory.workflows.casero_cta import split_cta_audio
from ai_video_factory.workflows.casero_cta_video import _cta_request


def _png(path: Path, color: str) -> None:
    Image.new("RGB", (64, 96), color).save(path, format="PNG")


def test_current_casero_cta_uses_full_hd_promoted_a2v_run_v2(tmp_path: Path) -> None:
    image = tmp_path / "promotion.png"
    audio = tmp_path / "first_half.wav"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")

    request = _cta_request(
        tmp_path,
        {"files": {"image": image, "first_half": audio}},
    )

    assert request.parameters["generation_profile"] == LTX_A2V_RUN_OPTIMIZED_PROFILE
    assert request.parameters["width"] == 1920
    assert request.parameters["height"] == 1080
    assert request.parameters["fps"] == 24
    assert request.parameters["vae_backend"] == LTX_A2V_RUN_VAE_BACKEND
    assert request.parameters["guidance_forward_batch_size"] == (
        LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE
    )
    assert request.parameters["progressive_block_group_size"] == (
        LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE
    )


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


def test_casero_cta_launches_concurrently_with_b11_and_professional_skips() -> None:
    """A one-slot B concurrency limit must not serialize B1.1 against CTA."""
    import asyncio

    from ai_video_factory.bots.contracts import B11Output
    from ai_video_factory.bots.workflow import run_b_pipeline

    source = "Intro one. Start main."
    valid_b11 = B11Output.model_validate({
        "pipeline_stage": "B1.1",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [
            {
                "block_id": 1, "type": "intro",
                "emotional_entry": "neutral", "emotional_exit": "ready",
                "span": {"first_words": "Intro one.", "last_words": "Intro one."},
            },
            {
                "block_id": 2, "type": "development",
                "emotional_entry": "ready", "emotional_exit": "focused",
                "span": {"first_words": "Start main.", "last_words": "Start main."},
            },
        ],
        "error": None,
    })

    class ConcurrentProvider:
        def __init__(self, concurrent: bool) -> None:
            self.concurrent = concurrent
            self.b11_started = asyncio.Event()
            self.cta_started = asyncio.Event()
            self.calls: list[str] = []

        async def generate_structured(self, **kwargs):
            output_type = kwargs["output_type"]
            self.calls.append(output_type.__name__)
            if output_type is B11Output:
                self.b11_started.set()
                if self.concurrent:
                    await asyncio.wait_for(self.cta_started.wait(), 1.0)
                return valid_b11
            assert output_type is CTAPlacement
            self.cta_started.set()
            await asyncio.wait_for(self.b11_started.wait(), 1.0)
            return CTAPlacement(
                previous_sentence="Intro one.", next_sentence="Start main.",
            )

    async def exercise(style: str, expected: list[str]) -> dict:
        provider = ConcurrentProvider(concurrent=style == "casero")
        outputs: dict[str, dict] = {}
        result = await asyncio.wait_for(
            run_b_pipeline(
                source, provider=provider, max_parallel_calls=1,
                visual_style=style,
                enable_casero_cta=True,
                stop_after="B1.1",
                on_output=lambda stage, block_id, payload: outputs.__setitem__(
                    stage, payload
                ),
            ),
            timeout=2.0,
        )
        assert result.b11.narrative_core.central_question == "Q"
        assert sorted(provider.calls) == sorted(expected)
        return outputs

    casero = asyncio.run(exercise("casero", ["B11Output", "CTAPlacement"]))
    assert casero["CTA"]["previous_sentence"] == "Intro one."
    assert casero["CTA"]["next_sentence"] == "Start main."
    assert casero["CTA"]["insert_after_character"] == len("Intro one.")
    professional = asyncio.run(exercise("profesional", ["B11Output"]))
    assert "CTA" not in professional
