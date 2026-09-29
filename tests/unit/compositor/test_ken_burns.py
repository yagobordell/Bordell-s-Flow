from __future__ import annotations

import pytest

from ai_video_factory.compositor.ken_burns import (
    MOTION_VERSION,
    build_clip_ken_burns_filter,
    build_timeline_ken_burns_filter,
    compute_zoom_start,
    motion_metadata,
)


def test_zoom_start_matches_media_pipeline_speed_per_second() -> None:
    assert compute_zoom_start(2.0) == pytest.approx(1.04)
    assert compute_zoom_start(6.0) == pytest.approx(1.12)


def test_motion_metadata_is_bottom_left_zoom_out_to_one() -> None:
    assert motion_metadata(2.0) == {
        "version": MOTION_VERSION,
        "mode": "ken_burns",
        "direction": "out",
        "origin": "bottom_left",
        "zoom_start": 1.04,
        "zoom_end": 1.0,
    }


def test_clip_filter_uses_scale_crop_instead_of_zoompan() -> None:
    filter_chain = build_clip_ken_burns_filter(
        width=1280,
        height=720,
        fps=24,
        duration_seconds=2.0,
    )

    assert "zoompan=" not in filter_chain
    assert filter_chain.startswith("scale=w='")
    assert ":eval=frame" in filter_chain
    assert "max(1280/iw\\,720/ih)" in filter_chain
    assert "1.040000+(1.000000-1.040000)" in filter_chain
    assert "crop=1280:720" in filter_chain
    assert "(in_w-out_w)*0.020000" in filter_chain
    assert "(in_h-out_h)*0.980000" in filter_chain
    assert "setsar=1" in filter_chain
    assert "fps=24" in filter_chain
    assert filter_chain.endswith("format=yuv420p")


def test_timeline_filter_materializes_frames_before_dynamic_scale() -> None:
    filter_chain = build_timeline_ken_burns_filter(
        [(0.5, 1.5)],
        width=1280,
        height=720,
        fps=30,
    )

    assert filter_chain.startswith("fps=30,scale=w='")
    assert "max(1280/iw\\,720/ih)*(1+" in filter_chain
    assert "zoompan=" not in filter_chain
    assert "gte(t\\,0.500000000)" in filter_chain
    assert "lt(t\\,1.500000000)" in filter_chain
    assert "crop=1280:720" in filter_chain
