"""Offline contracts for optional I2V camera guidance and isolated replay identity."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.workers.ltx25.jobs import ltx_video_application_job_id
from ai_video_factory.workers.ltx25.model import (
    LTX_GENERATION_PROFILE,
    LTX_PRODUCTION_I2V_PROFILE,
    LTX_STATIC_CAMERA_GENERATION_PROFILE,
    LTXVideoParameters,
)
from ai_video_factory.workflows.video_generation import build_video_generation_plan


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


@pytest.mark.parametrize(
    "prompt",
    [
        "An elderly monk speaking calmly to the camera.",
        "woman talks into a mic",
        "A woman whispering to the microphone",
    ],
)
def test_opt_in_profile_keeps_input_seed_and_prompt_but_uses_distinct_job_identity(
    tmp_path: Path, prompt: str
) -> None:
    image = tmp_path / "keyframe.png"
    Image.new("RGB", (1280, 720), (20, 30, 40)).save(image)
    args = (
        [StoryboardKeyframe(shot_id=1, uri=image.name)],
        [VideoPrompt(shot_id=1, prompt=prompt)],
        [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=2.0)],
    )
    kwargs = {"keyframe_base_dir": tmp_path, "seed_base": 4242}
    default = build_video_generation_plan(*args, **kwargs)[0].request
    explicit_default = build_video_generation_plan(
        *args, generation_profile=LTX_PRODUCTION_I2V_PROFILE, **kwargs
    )[0].request
    static = build_video_generation_plan(
        *args, generation_profile=LTX_STATIC_CAMERA_GENERATION_PROFILE, **kwargs
    )[0].request
    legacy = build_video_generation_plan(
        *args, generation_profile=LTX_GENERATION_PROFILE, **kwargs
    )[0].request
    assert default.job_id == explicit_default.job_id
    assert default.parameters["generation_profile"] == LTX_PRODUCTION_I2V_PROFILE
    assert legacy.parameters["generation_profile"] == LTX_GENERATION_PROFILE
    assert legacy.job_id != default.job_id
    assert legacy.fingerprint() != default.fingerprint()
    assert legacy.output.key != default.output.key
    assert default.fingerprint() == explicit_default.fingerprint()
    assert default.parameters["prompt"] == static.parameters["prompt"] == prompt
    assert default.parameters["seed"] == static.parameters["seed"] == 4243
    assert default.parameters["num_frames"] == static.parameters["num_frames"] == 49
    assert static.parameters["generation_profile"] == LTX_STATIC_CAMERA_GENERATION_PROFILE
    assert default.job_id != static.job_id
    assert default.fingerprint() != static.fingerprint()
    assert default.output.key != static.output.key
    assert default.inputs == static.inputs


def test_invalid_profile_never_creates_a_generation_plan(tmp_path: Path) -> None:
    image = tmp_path / "keyframe.png"
    Image.new("RGB", (1280, 720)).save(image)
    with pytest.raises(ValueError, match="Unsupported I2V generation profile"):
        build_video_generation_plan(
            [StoryboardKeyframe(shot_id=1, uri=image.name)],
            [VideoPrompt(shot_id=1, prompt="An unmoving camera.")],
            [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=2.0)],
            keyframe_base_dir=tmp_path,
            generation_profile="not-a-profile",
        )


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
