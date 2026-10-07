from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from ai_video_factory.compositor import static_preview
from ai_video_factory.compositor.ken_burns import (
    MOTION_VERSION,
    build_clip_ken_burns_filter,
    build_timeline_ken_burns_filter,
    compute_zoom_bounds,
    compute_zoom_start,
    motion_direction_for_index,
    motion_metadata,
)


def test_motion_direction_alternates_out_and_in() -> None:
    assert [motion_direction_for_index(index) for index in range(6)] == [
        "out", "in", "out", "in", "out", "in",
    ]
    with pytest.raises(ValueError, match="nonnegative"):
        motion_direction_for_index(-1)


def test_zoom_bounds_match_media_pipeline_speed_per_second() -> None:
    assert compute_zoom_start(2.0) == pytest.approx(1.04)
    assert compute_zoom_start(6.0) == pytest.approx(1.12)
    assert compute_zoom_bounds(2.0, direction="out") == pytest.approx((1.04, 1.0))
    assert compute_zoom_bounds(2.0, direction="in") == pytest.approx((1.0, 1.04))


def test_motion_metadata_carries_bidirectional_zoom_bounds() -> None:
    assert motion_metadata(2.0, direction="out") == {
        "version": MOTION_VERSION,
        "mode": "ken_burns",
        "direction": "out",
        "origin": "bottom_left",
        "zoom_start": 1.04,
        "zoom_end": 1.0,
    }
    assert motion_metadata(2.0, direction="in") == {
        "version": MOTION_VERSION,
        "mode": "ken_burns",
        "direction": "in",
        "origin": "bottom_left",
        "zoom_start": 1.0,
        "zoom_end": 1.04,
    }


def test_clip_filter_uses_fractional_corners_for_both_directions() -> None:
    zoom_out = build_clip_ken_burns_filter(
        width=1280,
        height=720,
        fps=24,
        duration_seconds=2.0,
        direction="out",
    )
    zoom_in = build_clip_ken_burns_filter(
        width=1280,
        height=720,
        fps=24,
        duration_seconds=2.0,
        direction="in",
    )

    for graph in (zoom_out, zoom_in):
        assert graph.startswith(
            "fps=24,scale=1280:720:flags=lanczos,format=yuv444p,perspective="
        )
        assert "zoompan=" not in graph
        assert "trunc(" not in graph
        assert "crop=" not in graph
        assert "eval=frame" in graph
        assert "(on-1)/47" in graph
        assert "*0.020000" in graph
        assert "*0.980000" in graph
        assert ":sense=source:interpolation=linear:eval=frame" in graph
        assert graph.endswith("setsar=1,format=yuv420p")

    assert "1.040000+(1.000000-1.040000)" in zoom_out
    assert "1.000000+(1.040000-1.000000)" in zoom_in


def test_timeline_filter_contains_inverse_zoom_curves() -> None:
    graph = build_timeline_ken_burns_filter(
        [(15, 45, "out"), (60, 90, "in")],
        width=1280,
        height=720,
        fps=30,
    )
    assert graph.startswith("fps=30,scale=1280:720:flags=lanczos,format=yuv444p,")
    assert "gte(on\\,16)" in graph
    assert "lt(on\\,46)" in graph
    assert "(on-16)/29" in graph
    assert "0.020000+(0.000000-0.020000)" in graph
    assert "gte(on\\,61)" in graph
    assert "lt(on\\,91)" in graph
    assert "(on-61)/29" in graph
    assert "0.000000+(0.020000-0.000000)" in graph
    assert ":sense=source:interpolation=linear:eval=frame" in graph


def test_invalid_intervals_and_empty_timeline() -> None:
    assert build_timeline_ken_burns_filter(
        [], width=1280, height=720, fps=30,
    ) == "fps=30,format=yuv420p"
    with pytest.raises(ValueError, match="at least two"):
        build_timeline_ken_burns_filter(
            [(0, 1, "out")], width=1280, height=720, fps=30,
        )


@pytest.mark.parametrize("direction", ["out", "in"])
def test_ffmpeg_fractional_ken_burns_preserves_exact_frame_count(
    tmp_path: Path,
    direction: str,
) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is required for a local, non-API motion regression")
    source = tmp_path / f"pattern-{direction}.png"
    image = Image.new("RGB", (160, 90), "white")
    draw = ImageDraw.Draw(image)
    for y in range(0, 90, 8):
        for x in range(0, 160, 8):
            draw.rectangle((x, y, x + 3, y + 3), fill=(25, 50, 160))
    image.save(source)

    graph = build_clip_ken_burns_filter(
        width=160,
        height=90,
        fps=24,
        duration_seconds=1.0,
        direction=direction,
    )
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-loop", "1", "-framerate", "24", "-i", str(source),
            "-vf", graph, "-frames:v", "24",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True, check=True,
    )
    frame_bytes = 160 * 90 * 3
    assert len(result.stdout) == 24 * frame_bytes
    first = Image.frombytes("RGB", (160, 90), result.stdout[:frame_bytes])
    last = Image.frombytes("RGB", (160, 90), result.stdout[-frame_bytes:])
    assert ImageChops.difference(first, last).getbbox() is not None


def test_static_preview_long_filter_uses_modern_ffmpeg_file_option(
    tmp_path: Path,
) -> None:
    graph = "x" * 16_001

    args = static_preview._ffmpeg_video_filter_args(graph, tmp_path)

    script = tmp_path / "ken_burns.fffilter"
    assert args == ["-/filter:v", str(script)]
    assert script.read_text(encoding="utf-8") == graph + "\n"
    assert "-filter_script:v" not in args


def test_static_preview_short_filter_stays_inline(tmp_path: Path) -> None:
    graph = "fps=30,format=yuv420p"

    args = static_preview._ffmpeg_video_filter_args(graph, tmp_path)

    assert args == ["-vf", graph]
    assert not (tmp_path / "ken_burns.fffilter").exists()
