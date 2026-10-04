"""Offline contracts for optional I2V camera guidance and isolated replay identity."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_video_factory.workers.ltx25.jobs import ltx_video_application_job_id
from ai_video_factory.workers.ltx25.model import (
    LTX_GENERATION_PROFILE,
    LTX_PRODUCTION_I2V_PROFILE,
    LTX_STATIC_CAMERA_GENERATION_PROFILE,
    LTXVideoParameters,
)


def test_static_camera_profile_is_opt_in_and_legacy_profile_is_supported() -> None:
    params = {
        "generation_profile": LTX_STATIC_CAMERA_GENERATION_PROFILE,
        "prompt": "A woman whispering to the microphone.",
        "seed": 4242,
        "width": 1280,
        "height": 720,
        "fps": 24,
        "num_frames": 49,
    }
    assert LTXVideoParameters.model_validate(params).prompt == params["prompt"]
    assert LTXVideoParameters.model_validate(
        {**params, "generation_profile": LTX_GENERATION_PROFILE}
    ).generation_profile == LTX_GENERATION_PROFILE
    with pytest.raises(ValidationError, match="generation_profile"):
        LTXVideoParameters.model_validate({**params, "generation_profile": "unknown"})


def test_static_camera_uses_real_guided_stage_one_and_unchanged_stage_two() -> None:
    source = Path("src/ai_video_factory/workers/ltx25/static_i2v.py").read_text(
        encoding="utf-8"
    )
    syntax = ast.parse(source)
    generator = next(
        node for node in syntax.body
        if isinstance(node, ast.FunctionDef) and node.name == "generate_static_camera"
    )
    calls = [node for node in ast.walk(generator) if isinstance(node, ast.Call)]
    invoked = [ast.unparse(node.func) for node in calls]
    assert invoked.count("GuidedDenoiser") == 1
    assert invoked.count("SimpleDenoiser") == 1
    assert "negative_context=negative_video_context" in source
    assert "[prompt, negative_prompt]" in source
    assert "cfg_scale=LTX_I2V_STATIC_CAMERA_VIDEO_CFG_SCALE" in source
    assert "if enhance_prompt:" in source
    assert source.index("denoiser=GuidedDenoiser") < source.index("denoiser=SimpleDenoiser")
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in syntax.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith("LTX_I2V_STATIC_CAMERA")
    }
    assert constants["LTX_I2V_STATIC_CAMERA_VIDEO_CFG_SCALE"] == 1.25
    assert constants["LTX_I2V_STATIC_CAMERA_NEGATIVE_PROMPT"] == (
        "camera zoom, zoom in, push in, dolly in, camera drift, camera shake, "
        "reframing, crop change, handheld camera, unintended camera movement, "
        "speaking directly to camera, talking directly to camera, addressing the viewer, "
        "direct-to-camera speech, presenter-style delivery, host-style delivery, "
        "vlog-style talking, monologue to camera, speaking into the lens, "
        "breaking the fourth wall, sustained eye contact with the lens while speaking"
    )
    assert "self._stage_1_sampler_kwargs(seed)" in source
    assert "self.video_decoder(" in source


def test_default_application_job_id_has_distinct_legacy_and_static_profiles() -> None:
    args = {
        "shot_id": 1,
        "prompt": "A stationary camera.",
        "keyframe_sha256": "a" * 64,
        "seed": 4243,
        "width": 1280,
        "height": 720,
        "fps": 24,
        "num_frames": 49,
    }
    baseline = ltx_video_application_job_id(**args)
    assert baseline == ltx_video_application_job_id(
        **args, generation_profile=LTX_PRODUCTION_I2V_PROFILE
    )
    assert baseline != ltx_video_application_job_id(
        **args, generation_profile=LTX_GENERATION_PROFILE
    )
    assert baseline != ltx_video_application_job_id(
        **args, generation_profile=LTX_STATIC_CAMERA_GENERATION_PROFILE
    )
