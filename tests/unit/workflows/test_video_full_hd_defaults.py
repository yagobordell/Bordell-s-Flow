"""Cheap source-contract tests: new video outputs default Full HD; old jobs remain 720p.

These tests verify request dimensions and composition contracts only. The
32-GiB RTX 5090 memory and A2V lip-sync quality gates require paid GPU tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.compositor import b_video as compositor
from ai_video_factory.video_resolution import validate_video_size
from ai_video_factory.workers.ltx25.a2v import LTXAudioToVideoParameters
from ai_video_factory.workers.ltx25.model import (
    LTX_PRODUCTION_I2V_PROFILE,
    LTXVideoParameters,
    _pipeline_dimensions,
    _prepare_grid_keyframe,
)
from ai_video_factory.workflows.casero_cta_video import (
    CTACompositionError,
    _assert_cta_size,
    _cta_request,
    compose_reusable_cta,
)


def _i2v(width: int, height: int) -> LTXVideoParameters:
    return LTXVideoParameters(
        generation_profile=LTX_PRODUCTION_I2V_PROFILE,
        prompt="A subject looks to the left.",
        seed=5243,
        width=width, height=height, fps=24, num_frames=121,
    )


def test_delivery_presets_require_explicit_legacy_720p() -> None:
    validate_video_size(1920, 1080)
    validate_video_size(1280, 720)
    for width, height, fps in ((1080, 1920, 24), (1920, 1088, 24), (1920, 1080, 30)):
        with pytest.raises(ValueError, match="delivery"):
            validate_video_size(width, height, fps)


def test_full_hd_i2v_uses_grid_padding_and_unchanged_legacy_720p() -> None:
    assert _pipeline_dimensions(_i2v(1920, 1080)) == (1920, 1088)
    assert _pipeline_dimensions(_i2v(1280, 720)) == (1280, 768)
    with pytest.raises(ValueError, match="divisible by 64"):
        _i2v(1920, 1081)


def test_720p_keyframe_is_upscaled_as_conditioning_with_deterministic_padding(
    tmp_path: Path,
) -> None:
    image = tmp_path / "previous-720.png"
    Image.new("RGB", (1280, 720), (40, 80, 120)).save(image)
    result = _prepare_grid_keyframe(
        image, tmp_path / "full-hd-grid.png",
        requested_width=1920, requested_height=1080,
        pipeline_width=1920, pipeline_height=1088,
    )
    with Image.open(result) as prepared:
        assert prepared.size == (1920, 1088)
        assert prepared.getpixel((200, 540)) == (40, 80, 120)
        assert prepared.getpixel((200, 0)) == (40, 80, 120)
        assert prepared.getpixel((200, 1087)) == (40, 80, 120)


def test_new_a2v_is_full_hd_but_explicit_legacy_request_is_preserved() -> None:
    new = LTXAudioToVideoParameters()
    old = LTXAudioToVideoParameters(width=1280, height=720)
    assert (new.width, new.height, new.fps) == (1920, 1080, 24)
    assert (old.width, old.height, old.fps) == (1280, 720, 24)


def test_cta_output_resolution_changes_immutable_paid_job_identity(tmp_path: Path) -> None:
    image = tmp_path / "cta.png"
    audio = tmp_path / "cta.wav"
    image.write_bytes(b"valid-hash-image")
    audio.write_bytes(b"valid-hash-audio")
    assets = {"files": {"image": image, "first_half": audio}}
    full_hd = _cta_request(tmp_path, assets)
    legacy = _cta_request(tmp_path, assets, video_size=(1280, 720))
    assert (full_hd.parameters["width"], full_hd.parameters["height"]) == (1920, 1080)
    assert (legacy.parameters["width"], legacy.parameters["height"]) == (1280, 720)
    assert full_hd.job_id != legacy.job_id
    assert full_hd.fingerprint() != legacy.fingerprint()


def test_cta_legacy_cache_never_silently_passes_for_full_hd() -> None:
    # CTA completed.json stored by earlier versions had no width/height.
    previous = {"status": "completed", "schema_version": "casero-cta-final-v1"}
    _assert_cta_size(previous, (1280, 720))
    with pytest.raises(CTACompositionError, match="fresh CTA asset"):
        _assert_cta_size(previous, (1920, 1080))


def test_compositor_checks_full_hd_or_explicit_legacy_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command, **kwargs):
        del command, kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080,
                 "avg_frame_rate": "24/1", "nb_frames": "24"},
                {"codec_type": "audio"},
            ],
            "format": {"duration": "1.0"},
        }))

    monkeypatch.setattr(compositor.subprocess, "run", fake_run)
    assert compositor._probe_final(Path("dummy.mp4"), 1.0, 24) == 1.0
    with pytest.raises(compositor.B2VideoCompositionError, match="1280x720"):
        compositor._probe_final(Path("dummy.mp4"), 1.0, 24, width=1280, height=720)
    assert "scale=960:1080:" in compositor._fit(960)


def test_casero_cta_full_hd_composition_preserves_exact_1080p(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_video_factory.workflows.casero_cta_video as cta

    recorded = []

    def fake_encode(command: list[str]) -> None:
        recorded.append(command)
        Path(command[-1]).write_bytes(b"full-hd-cta-test-video")

    monkeypatch.setattr(cta, "_ffmpeg", fake_encode)
    assets = {
        "half_seconds": 2.0,
        "duration_seconds": 4.0,
        "files": {
            "motion": tmp_path / "motion.mp4",
            "master": tmp_path / "speech.wav",
        },
    }
    output = compose_reusable_cta(tmp_path, assets)
    assert output.is_file()
    filters = recorded[0][recorded[0].index("-filter_complex") + 1]
    assert filters.count("scale=1920:1080") == 2
    receipt = json.loads((tmp_path / "completed.json").read_text(encoding="utf-8"))
    assert (receipt["width"], receipt["height"]) == (1920, 1080)
    with pytest.raises(CTACompositionError, match="1280x720"):
        compose_reusable_cta(tmp_path, video_size=(1280, 720))
