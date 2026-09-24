from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _ltx_a2v_support import _seed_model_files, make_a2v_bindings
from PIL import Image

import ai_video_factory.workers.ltx25.a2v as a2v
from ai_video_factory.workers.ltx25 import (
    DirectLTX25AudioToVideoBackend,
    LTXAudioToVideoParameters,
)


def test_direct_a2v_uses_official_pipeline_audio_duration_and_mux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_root = tmp_path / "models"
    _seed_model_files(model_root)
    image = tmp_path / "avatar.png"
    Image.new("RGB", (900, 1200), (80, 100, 120)).save(image)
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fake-audio")

    state: dict[str, Any] = {
        "builds": 0,
        "calls": [],
        "encodes": [],
        "conditionings": [],
        "inference_depth": 0,
        "inference_entries": 0,
    }

    bindings = make_a2v_bindings(state)
    monkeypatch.setattr(a2v, "_load_a2v_bindings", lambda: bindings)
    monkeypatch.setattr(
        a2v,
        "probe_audio",
        lambda _: a2v.AudioProbe("pcm_s16le", 24000, 1, 4.0),
    )
    monkeypatch.setattr(
        a2v,
        "_prepare_pipeline_audio",
        lambda source, destination, *, probe: (
            source,
            a2v.AudioProbe(
                probe.codec,
                probe.sample_rate,
                2,
                probe.duration_seconds,
            ),
        ),
    )
    monkeypatch.setattr(a2v, "_output_duration", lambda _: 3.708333)

    backend = DirectLTX25AudioToVideoBackend(model_root=model_root)
    backend.prepare()
    metadata = backend.generate(
        image_path=image,
        audio_path=audio,
        output_path=tmp_path / "output.mp4",
        parameters=LTXAudioToVideoParameters(prompt="A stable talking head."),
    )
    backend.generate(
        image_path=image,
        audio_path=audio,
        output_path=tmp_path / "second.mp4",
        parameters=LTXAudioToVideoParameters(prompt="A stable talking head."),
    )

    assert state["builds"] == 1
    assert state["pipeline_init"]["quantization"] == "fp8-policy"
    assert state["pipeline_init"]["offload_mode"] == "cpu"
    assert "distilled-transformer" in state["model_paths"]["transformer_path"]
    assert state["pipeline_init"]["distilled_lora"] == []
    assert state["calls"][0]["num_frames"] is None
    assert state["calls"][0]["num_inference_steps"] == 8
    assert state["calls"][0]["stage_1_sigmas"] == bindings.distilled_sigmas
    assert state["calls"][0]["stage_2_sigmas"] == bindings.stage_2_sigmas
    guider = state["calls"][0]["video_guider_params"]
    assert guider.cfg_scale == 1.0
    assert guider.rescale_scale == 0.0
    assert guider.stg_scale == 0.0
    assert guider.stg_blocks == []
    assert guider.modality_scale == 1.0
    assert state["original_guider"].modality_scale == 3.0
    assert state["original_guider"].stg_scale == 1.0
    assert state["calls"][1]["video_guider_params"] == guider
    assert state["calls"][0]["height"] == 768
    assert state["tiling_budget"] == 20_000_000_000
    assert len(state["cleanup_devices"]) == 2
    assert state["encodes"][0]["audio"] is not None
    assert list(state["encodes"][0]["video"]) == ["cropped"]
    assert metadata["input_audio_duration_seconds"] == 4.0
    assert metadata["input_audio_channels"] == 1
    assert metadata["conditioning_audio_channels"] == 2
    assert metadata["audio_upmixed_to_stereo"] is True
    assert metadata["effective_audio_duration_seconds"] == pytest.approx(89000 / 24000)
    assert metadata["num_frames"] == 89
    assert metadata["video_cfg_scale"] == 1.0
    assert metadata["stage_1_steps"] == 8
    assert metadata["stage_2_steps"] == 3
    assert metadata["transformer_variant"] == "distilled"
    assert metadata["video_stg_scale"] == 0.0
    assert metadata["video_modality_scale"] == 1.0
    assert metadata["generation_profile"].endswith("-v5")
    assert metadata["peak_vram_bytes"] == 123456
    assert metadata["pipeline_reused"] is False
    assert state["conditionings"][0]["strength"] == 1.0
    assert state["inference_entries"] == 2
    assert state["inference_depth"] == 0

    # Changing profiles must rebuild the correct transformer instead of reusing
    # the cached distilled pipeline with incompatible dev sigmas/LoRA.
    dev_metadata = backend.generate(
        image_path=image,
        audio_path=audio,
        output_path=tmp_path / "dev.mp4",
        parameters=LTXAudioToVideoParameters(
            generation_profile=a2v.LTX_A2V_DEV_GENERATION_PROFILE,
            prompt="A stable talking head.",
        ),
    )
    assert state["builds"] == 2
    assert "dev-transformer" in state["pipeline_inits"][1]["model_paths"]["transformer_path"]
    assert len(state["pipeline_inits"][1]["distilled_lora"]) == 1
    assert state["calls"][2]["num_inference_steps"] == 30
    assert state["calls"][2]["stage_1_sigmas"] is None
    assert state["calls"][2]["video_guider_params"].cfg_scale == 3.0
    assert dev_metadata["generation_profile"] == a2v.LTX_A2V_DEV_GENERATION_PROFILE
    assert dev_metadata["transformer_variant"] == "dev"
    assert dev_metadata["stage_1_steps"] == 30

    backend.generate(
        image_path=image,
        audio_path=audio,
        output_path=tmp_path / "fast-again.mp4",
        parameters=LTXAudioToVideoParameters(prompt="A stable talking head."),
    )
    assert state["builds"] == 3
    assert state["pipeline_inits"][2]["distilled_lora"] == []
    assert state["calls"][3]["num_inference_steps"] == 8
