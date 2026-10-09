"""The ebook URL is timed by spoken QR, separately from the QR itself."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.compositor.professional_cta_qr import (
    ProfessionalCTAOverlayError,
    apply_professional_ebook_qr,
    professional_cta_website_windows,
    professional_cta_windows,
)
from ai_video_factory.inference.storage import sha256_file


def _timeline(*, first_qr=True, second_qr=True):
    return {
        "duration_seconds": 220.0,
        "beats": [
            {
                "beat_id": "1A", "start_seconds": 0.0,
                "end_seconds": 10.0,
                # This is outside a CTA, and must not trigger the URL.
                "qr_spoken_seconds": [3.5],
            },
            {
                "beat_id": "1B", "start_seconds": 10.0,
                "end_seconds": 25.0,
                **({"qr_spoken_seconds": [12.37, 20.5]} if first_qr else {}),
            },
            {"beat_id": "1C", "start_seconds": 25.0, "end_seconds": 35.0},
            {"beat_id": "2A", "start_seconds": 35.0, "end_seconds": 175.0},
            {
                "beat_id": "2B", "start_seconds": 175.0,
                "end_seconds": 185.0,
                **({"qr_spoken_seconds": [179.125]} if second_qr else {}),
            },
            {"beat_id": "2C", "start_seconds": 185.0, "end_seconds": 220.0},
        ],
    }


def _plan():
    return {"ebook_cta_groups": [["1B", "1C"], ["2B"]]}


def test_url_starts_at_first_spoken_qr_per_cta_and_ends_at_cta_end():
    assert professional_cta_website_windows(_timeline(), _plan()) == [
        (12.37, 35.0),
        (179.125, 185.0),
    ]
    assert professional_cta_windows(_timeline(), _plan()) == [
        (10.0, 125.0),
        (175.0, 185.0),
    ]


def test_missing_spoken_qr_never_shows_website_in_that_cta():
    assert professional_cta_website_windows(
        _timeline(first_qr=False), _plan(),
    ) == [(179.125, 185.0)]
    assert professional_cta_website_windows(
        _timeline(first_qr=False, second_qr=False), _plan(),
    ) == []
    assert professional_cta_windows(
        _timeline(first_qr=False, second_qr=False), _plan(),
    ) == [(10.0, 125.0), (175.0, 185.0)]


@pytest.mark.parametrize("value", [-1, 40, float("nan"), float("inf"), True])
def test_invalid_spoken_word_timings_fail_closed(value):
    timeline = _timeline()
    timeline["beats"][1]["qr_spoken_seconds"] = [value]
    with pytest.raises(ProfessionalCTAOverlayError, match="Invalid STT QR onset"):
        professional_cta_website_windows(timeline, _plan())


def _compose(tmp_path, monkeypatch, *, first_qr=True, second_qr=True):
    from ai_video_factory.compositor import b_video, professional_cta_qr

    output = tmp_path / "run"
    run_dir = output / "video" / "runs" / "test"
    run_dir.mkdir(parents=True)
    qr = output / "qr.png"
    Image.new("RGB", (100, 100), "white").save(qr)
    source = output / "original.mp4"
    source.write_bytes(b"fake verified mux")
    commands = []

    def fake_run(args, **kwargs):
        assert kwargs["check"] is False
        commands.append(args)
        output_video = args[-1]
        from pathlib import Path

        Path(output_video).write_bytes(b"composited video with QR and website")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(professional_cta_qr.subprocess, "run", fake_run)
    monkeypatch.setattr(b_video, "_probe_final", lambda *args: None)
    result = apply_professional_ebook_qr(
        output, run_dir,
        {"file": source.name, "sha256": sha256_file(source)},
        _timeline(first_qr=first_qr, second_qr=second_qr),
        {
            **_plan(),
            "avatar": {"qr": {"sha256": sha256_file(qr)}},
        },
    )
    assert len(commands) == 1
    return result, commands[0], run_dir


def test_website_is_small_white_outlined_image_above_bottom_subtitles(
    tmp_path, monkeypatch,
):
    result, cmd, run_dir = _compose(tmp_path, monkeypatch)
    filter_graph = cmd[cmd.index("-filter_complex") + 1]
    assert "between(t\\,10.000000\\,125.000000)" in filter_graph
    assert "between(t\\,175.000000\\,185.000000)" in filter_graph
    assert "gte(t\\,12.370000)*lt(t\\,35.000000)" in filter_graph
    assert "gte(t\\,179.125000)*lt(t\\,185.000000)" in filter_graph
    assert "x=(main_w-overlay_w)/2:y=main_h-overlay_h-145" in filter_graph
    assert cmd.count("-loop") == 2  # One real QR, one rasterized URL
    meta = result["professional_ebook_qr"]
    assert meta["first_cta_tail_seconds"] == 90
    website = meta["website_overlay"]
    assert website["text"] == "zellenne.com"
    assert website["font_size_pixels"] == 46
    assert website["black_outline_pixels"] == 2
    assert website["windows_seconds"] == [[12.37, 35.0], [179.125, 185.0]]
    image = Image.open(run_dir / "ebook_cta_website.png").convert("RGBA")
    pixels = list(image.getdata())
    assert any(r == g == b == 255 and a == 255 for r, g, b, a in pixels)
    assert any(r == g == b == 0 and a == 255 for r, g, b, a in pixels)
    assert any(a == 0 for _r, _g, _b, a in pixels)


def test_no_spoken_qr_uses_only_existing_qr_overlay(tmp_path, monkeypatch):
    result, cmd, run_dir = _compose(
        tmp_path, monkeypatch, first_qr=False, second_qr=False,
    )
    assert cmd.count("-loop") == 1
    assert not (run_dir / "ebook_cta_website.png").exists()
    assert result["professional_ebook_qr"]["website_overlay"]["windows_seconds"] == []
