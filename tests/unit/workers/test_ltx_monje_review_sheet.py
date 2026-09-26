from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from scripts.diagnostics import ltx25_monje_review_sheet as review


def test_review_samples_include_last_decodable_frame() -> None:
    times = review.sample_times(121 / 24, 24, count=6)
    assert len(times) == 6
    assert times[0] == 0.0
    assert times[-1] == pytest.approx(5.0)
    assert all(a < b for a, b in zip(times, times[1:], strict=False))
    with pytest.raises(ValueError, match="positive"):
        review.sample_times(0, 24)


def test_review_sheet_is_offline_and_does_not_modify_source_mp4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "avatar_segment.mp4"
    video.write_bytes(b"do-not-modify-the-speech-or-video")
    sheet = tmp_path / "monje_framing_review.png"
    monkeypatch.setattr(review, "probe_video", lambda _: (121 / 24, 24))

    def fake_extract(_video: Path, _time: float, _destination: Path) -> Image.Image:
        return Image.new("RGB", (1280, 720), (55, 61, 65))

    monkeypatch.setattr(review, "_extract_frame", fake_extract)
    report = review.create_review_sheet(video, sheet)
    assert sheet.is_file()
    with Image.open(sheet) as result:
        assert result.size == (1976, 444)
    assert video.read_bytes() == b"do-not-modify-the-speech-or-video"
    assert report["zoom_assessment"] == "manual_review_required"
    assert report["output_video_modified"] is False
    assert json.loads(sheet.with_suffix(".json").read_text(encoding="utf-8")) == report


def test_review_script_never_replaces_end_frame_or_stabilizes_mp4() -> None:
    script = Path("scripts/diagnostics/ltx25_monje_review_sheet.py").read_text(
        encoding="utf-8"
    )
    assert "last_frame_time" in script
    assert "zoom_assessment" in script
    assert "output_video_modified" in script
    assert "ffmpeg" in script
    assert "draw.text" in script

