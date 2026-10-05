"""Offline contract tests for grounded, reusable casero CTA motion copy."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from ai_video_factory.bots.cta_motion_copy import CTAMotionCopy
from ai_video_factory.compositor.cta_motion import (
    H,
    W,
    _copy_layout,
    _render_frame,
)
from ai_video_factory.workflows import casero_cta as workflow
from ai_video_factory.workflows import casero_cta_video as video


def _png(path: Path) -> None:
    Image.new("RGB", (240, 360), "white").save(path, format="PNG")


def _save(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("payload", [
    {"title": "", "benefits": []},
    {"title": " " + "Libro", "benefits": []},
    {"title": "A" * 73, "benefits": []},
    {"title": "Libro", "benefits": ["x" * 53]},
    {"title": "Libro", "benefits": ["bien", "BIEN"]},
    {"title": "Libro", "benefits": ["uno", "dos", "tres", "cuatro"]},
    {"title": "Libro", "benefits": [], "qr_text": "Override fixed English label"},
])
def test_motion_copy_rejects_invalid_fields(payload: dict) -> None:
    with pytest.raises(ValidationError):
        CTAMotionCopy.model_validate(payload)


def test_motion_copy_can_be_grounded_without_invented_benefits() -> None:
    assert CTAMotionCopy(title="Mi guía", benefits=[]).model_dump() == {
        "title": "Mi guía", "benefits": [],
    }


def test_renderer_shows_book_specific_copy_but_owns_fixed_english_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_video_factory.compositor.cta_motion as motion

    cover = Image.new("RGB", (180, 260), "white")
    qr = Image.new("RGB", (300, 300), "white")
    copy = CTAMotionCopy(
        title="Guía de boda", benefits=["Planificación", "Lista de invitados"],
    )
    font_layout = _copy_layout(copy, 900)
    assert len(font_layout[0]) == 1
    assert len(font_layout[2]) == 2
    captured: list[str] = []
    real_draw = motion.ImageDraw.Draw

    def spy_draw(img, *args, **kwargs):
        drawer = real_draw(img, *args, **kwargs)
        real_text = drawer.text

        def spy_text(xy, txt, *a, **kw):
            captured.append(txt)
            return real_text(xy, txt, *a, **kw)

        drawer.text = spy_text
        return drawer

    monkeypatch.setattr(motion.ImageDraw, "Draw", spy_draw)
    frame = _render_frame(
        6, background=Image.new("RGBA", (W, H), "black"), cover=cover,
        qr=qr, domain="ejemplo.com", copy=copy, layout=font_layout,
    )
    assert frame.size == (1920, 1080)
    assert {"THE BOOK", "SCAN FOR YOUR COPY", "SCAN WITH YOUR",
            "MOBILE PHONE", "Guía de boda", "Planificación",
            "Lista de invitados"} <= set(captured)
    assert "ejemplo.com" in captured


def test_motion_text_bot_uses_saved_spoken_cta_and_only_primary_cover(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cta"
    cache.mkdir()
    cover = tmp_path / "book.png"
    _png(cover)
    responses = []

    class Provider:
        async def generate_structured_with_response(self, **kwargs):
            responses.append(kwargs)
            return CTAMotionCopy(title="Libro visible", benefits=["Tema de portada"]), {
                "usage": {"input_tokens": 22, "output_tokens": 16},
            }

    calls = []
    first = asyncio.run(workflow._motion_text(
        cache, "Locución guardada", cover, Provider(), "test-model",
        lambda name, block, response: calls.append((name, block, response)),
    ))
    second = asyncio.run(workflow._motion_text(
        cache, "Locución guardada", cover, Provider(), "test-model", None,
    ))
    assert first == second
    assert len(responses) == 1
    assert responses[0]["output_type"] is CTAMotionCopy
    assert json.loads(responses[0]["input_text"]) == {"spoken_cta": "Locución guardada"}
    assert responses[0]["input_image_url"].startswith("data:image/png;base64,")
    assert len(calls) == 1 and calls[0][0] == "CTA Motion"
    assert _sha(cache / "motion_copy.json") == json.loads(
        (cache / "motion_copy_state.json").read_text()
    )["output_sha256"]


def test_motion_copy_unknown_paid_request_is_not_repeated(tmp_path: Path) -> None:
    directory = tmp_path / "cta"
    directory.mkdir()
    cover = tmp_path / "cover.png"
    _png(cover)
    _save(directory / "motion_copy_state.json", {"status": "request_started_unknown"})

    class Provider:
        async def generate_structured(self, **_):
            pytest.fail("Must not repeat uncertain billed request")

    with pytest.raises(workflow.CaseroCTAError, match="reconcile"):
        asyncio.run(workflow._motion_text(
            directory, "CTA", cover, Provider(), "model", None,
        ))


def test_current_cta_replaces_legacy_completion_only_after_full_hd_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "cta"
    directory.mkdir()
    legacy = directory / "final_cta.mp4"
    legacy.write_bytes(b"legacy 720p completion")
    _save(directory / "completed.json", {
        "schema_version": "casero-cta-final-v2",
        "status": "completed",
        "final": {"file": legacy.name, "sha256": _sha(legacy)},
    })
    a2v = directory / video._RUN_A2V_VIDEO
    a2v.write_bytes(b"new run-v2 a2v")
    motion = directory / "motion.mp4"
    motion.write_bytes(b"motion")
    master = directory / "master.wav"
    master.write_bytes(b"master")
    commands: list[list[str]] = []

    def fake_ffmpeg(arguments: list[str]) -> None:
        commands.append(arguments)
        Path(arguments[-1]).write_bytes(b"full hd rendered CTA")

    monkeypatch.setattr(video, "_ffmpeg", fake_ffmpeg)
    result = video.compose_reusable_cta(
        directory,
        {
            "files": {"motion": motion, "master": master},
            "half_seconds": 2.0,
            "duration_seconds": 4.0,
        },
    )

    assert result.name == video._RUN_FINAL_VIDEO
    assert result.read_bytes() == b"full hd rendered CTA"
    assert legacy.read_bytes() == b"legacy 720p completion"
    assert len(commands) == 1
    filter_complex = commands[0][commands[0].index("-filter_complex") + 1]
    assert "scale=1920:1080" in filter_complex
    completed = json.loads((directory / "completed.json").read_text())
    assert completed["schema_version"] == video._RUN_SCHEMA
    assert completed["width"] == 1920
    assert completed["height"] == 1080
    assert completed["final"]["file"] == video._RUN_FINAL_VIDEO
    assert completed["final"]["sha256"] == _sha(result)


def test_motion_v2_state_and_local_legacy_upgrade_preserve_old_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "cta"
    directory.mkdir()
    cover, qr = tmp_path / "cover.png", tmp_path / "qr.png"
    _png(cover)
    _png(qr)
    copy = CTAMotionCopy(title="Mi ebook", benefits=[])
    _save(directory / "motion_copy.json", copy.model_dump())
    writes = []

    def fake_motion(cov, qr_file, qr_url, seconds, target, *, copy):
        writes.append((cov, qr_file, qr_url, seconds, copy))
        target.write_bytes(b"new motion")

    monkeypatch.setattr(workflow, "build_motion", fake_motion)
    workflow._motion(directory, cover, qr, "https://example.com", 6.0, copy)
    workflow._motion(directory, cover, qr, "https://example.com", 6.0, copy)
    assert len(writes) == 1
    assert writes[0][3] == 7.5
    assert (directory / "motion_v2.mp4").read_bytes() == b"new motion"
    assert _sha(directory / "motion_v2.mp4") == json.loads(
        (directory / "motion_v2_state.json").read_text()
    )["motion"]["sha256"]

    def put(name: str, data: bytes) -> Path:
        path = directory / name
        path.write_bytes(data)
        return path

    old = put("final_cta.mp4", b"untouched original CTA")
    a2v = put("avatar_a2v.mp4", b"existing verified A2V")
    master = put("cta.wav", b"existing master narration")
    first = put("first_half.wav", b"first half")
    stt = put("stt.json", b"{}")
    promotion = put("promotion.png", b"existing promotion")
    _save(directory / "identity.json", {"avatar_sha256": "a", "qr_sha256": "b"})
    _save(directory / "completed.json", {
        "schema_version": "casero-cta-final-v1", "status": "completed",
        "final": {"file": old.name, "sha256": _sha(old)},
    })
    _save(directory / "a2v_state.json", {
        "status": "completed", "sha256": _sha(a2v),
    })
    _save(directory / "tts_state.json", {
        "status": "completed", "duration_seconds": 12., "half_seconds": 6.,
        "master": {"file": master.name, "sha256": _sha(master)},
        "first_half": {"file": first.name, "sha256": _sha(first)},
    })
    _save(directory / "stt_state.json", {
        "status": "completed",
        "transcript": {"file": stt.name, "sha256": _sha(stt)},
    })
    _save(directory / "image_state.json", {
        "status": "completed",
        "image": {"file": promotion.name, "sha256": _sha(promotion)},
    })

    def fake_ffmpeg(arguments):
        Path(arguments[-1]).write_bytes(b"upgraded local render")

    monkeypatch.setattr(video, "_ffmpeg", fake_ffmpeg)
    new = video.upgrade_cached_cta(directory)
    assert new.name == "final_cta_v2.mp4"
    assert new.read_bytes() == b"upgraded local render"
    assert old.read_bytes() == b"untouched original CTA"
    completed = json.loads((directory / "completed.json").read_text())
    assert completed["schema_version"] == "casero-cta-final-v2"
    assert completed["motion_copy_sha256"] == _sha(directory / "motion_copy.json")
    assert completed["final"]["sha256"] == _sha(new)


def test_legacy_cta_upgrade_refuses_missing_paid_a2v(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "cta"
    directory.mkdir()
    old = directory / "final_cta.mp4"
    old.write_bytes(b"old")
    _save(directory / "completed.json", {
        "schema_version": "casero-cta-final-v1",
        "final": {"file": old.name, "sha256": _sha(old)},
    })
    _save(directory / "a2v_state.json", {"status": "completed", "sha256": "unknown"})
    with pytest.raises(video.CTACompositionError, match="without resubmitting"):
        video.upgrade_cached_cta(directory)
    assert old.read_bytes() == b"old"


def test_completed_legacy_cta_only_bills_new_motion_bot_not_old_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.providers.ebook_covers import EbookCovers

    parent = tmp_path / "Layla"
    directory = parent / "meta" / "cta"
    directory.mkdir(parents=True)
    avatar = parent / "Layla.png"
    cover = tmp_path / "book.png"
    qr = parent / "meta" / "qr.png"
    for path in (avatar, cover, qr):
        _png(path)
    covers = EbookCovers(files=(cover,), sha256=(_sha(cover),))
    _save(directory / "identity.json", {
        "schema_version": "casero-cta-v1",
        "avatar_sha256": _sha(avatar),
        "cover_sha256": list(covers.sha256),
        "qr_sha256": _sha(qr),
        "voice_id": "voice-1", "voice_provider": "fishaudio",
        "qr_url": "https://example.com/book",
    })
    old = directory / "final_cta.mp4"
    old.write_bytes(b"old final")
    _save(directory / "completed.json", {
        "schema_version": "casero-cta-final-v1",
        "final": {"file": old.name, "sha256": _sha(old)},
    })
    (directory / "avatar_a2v.mp4").write_bytes(b"verified paid video")
    _save(directory / "a2v_state.json", {
        "status": "completed", "sha256": _sha(directory / "avatar_a2v.mp4"),
    })
    _save(directory / "script.json", {"cta": "Read the book to learn."})
    for filename in ("cta.wav", "first_half.wav"):
        (directory / filename).write_bytes(b"saved speech")
    _save(directory / "tts_state.json", {
        "status": "completed",
        "half_seconds": 6., "duration_seconds": 12.,
        "master": {"file": "cta.wav", "sha256": _sha(directory / "cta.wav")},
        "first_half": {
            "file": "first_half.wav", "sha256": _sha(directory / "first_half.wav"),
        },
    })
    for name in ("_tts", "_image", "_stt"):
        monkeypatch.setattr(
            workflow, name, lambda *a, **kw: pytest.fail("Legacy paid media reissued"),
        )
    rendered = []
    monkeypatch.setattr(
        workflow, "_motion",
        lambda *args: rendered.append(args),
    )
    monkeypatch.setattr(
        video, "upgrade_cached_cta", lambda _: old,
    )

    class Provider:
        calls = 0

        async def generate_structured(self, **kwargs):
            self.calls += 1
            assert kwargs["output_type"] is CTAMotionCopy
            return CTAMotionCopy(title="Visible cover", benefits=[])

    provider = Provider()
    args = {
        "avatar": avatar, "covers": covers, "qr_path": qr,
        "qr_url": "https://example.com/book",
        "script": "The current video has a completely different introduction.",
        "voice_id": "voice-1", "provider": provider, "model": "test",
    }
    first = asyncio.run(workflow.prepare_casero_cta(**args))
    second = asyncio.run(workflow.prepare_casero_cta(**args))
    assert first["status"] == second["status"] == "completed"
    assert first["final"]["file"] == "final_cta.mp4"
    assert len(rendered) == 2
    assert provider.calls == 1
    assert old.read_bytes() == b"old final"


def test_motion_stage_is_counted_in_api_costs_and_wall_clock() -> None:
    from types import SimpleNamespace

    from ai_video_factory.bots.billing import ApiCostLedger
    from scripts.pipeline.run_b_pipeline import TimingLedger

    usage = SimpleNamespace(
        input_tokens=200, output_tokens=50,
        input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )
    response = SimpleNamespace(
        id="motion-response", model="gpt-6-luna", service_tier="default",
        usage=usage,
    )
    ledger = ApiCostLedger()
    ledger.record("CTA Motion", None, response)
    report = ledger.report(blocks=0, run_status="completed", expected_calls={
        "B1.1": 0, "CTA": 0, "CTA Ebook": 0, "CTA Motion": 1,
        "B1.2": 0, "B2": 0,
    })
    assert report["stages"]["CTA Motion"]["recorded_calls"] == 1
    assert report["requests"][0]["stage"] == "CTA Motion"
    timers = TimingLedger()
    timers.record_call("CTA Motion", None, 0.5)
    assert timers.call_count("CTA Motion") == 1
    assert timers.report(run_seconds=1.0, status="completed")[
        "stages"
    ]["CTA Motion"]["elapsed_seconds"] is None
