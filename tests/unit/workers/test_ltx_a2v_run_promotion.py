from __future__ import annotations

import wave
from pathlib import Path

import ai_video_factory.workflows.b_video as b_video
from ai_video_factory.workers.ltx25 import (
    LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE,
    LTX_A2V_RUN_OPTIMIZED_PROFILE,
    LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE,
    LTX_A2V_RUN_VAE_BACKEND,
)


def test_run_builds_a2v_request_with_promoted_run_v2_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    avatar = tmp_path / "avatar.png"
    avatar.write_bytes(b"avatar")
    master = tmp_path / "master.wav"
    with wave.open(str(master), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x00\x00" * 48000)

    timeline = {
        "duration_seconds": 1.0,
        "beats": [
            {
                "block_id": 1,
                "beat_id": "beat-1",
                "visual_type": "avatar",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
            }
        ],
    }
    monkeypatch.setattr(
        b_video,
        "build_stt_beat_timeline",
        lambda *_args, **_kwargs: timeline,
    )
    monkeypatch.setattr(
        b_video,
        "_source_assets",
        lambda *_args, **_kwargs: (avatar, {}),
    )
    monkeypatch.setattr(
        b_video,
        "_verified_asset",
        lambda *_args, **_kwargs: master,
    )

    _timeline, jobs, _run_dir = b_video.build_b2_video_plan(
        output,
        {},
        {"file": "master.wav", "sha256": "a" * 64},
        {"status": "completed"},
    )

    assert len(jobs) == 1
    request = jobs[0].request
    assert jobs[0].kind == "a2v"
    assert request.parameters["generation_profile"] == LTX_A2V_RUN_OPTIMIZED_PROFILE
    assert request.parameters["vae_backend"] == LTX_A2V_RUN_VAE_BACKEND
    assert (
        request.parameters["guidance_forward_batch_size"]
        == LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE
        == 4
    )
    assert (
        request.parameters["progressive_block_group_size"]
        == LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE
        == 8
    )
    assert request.parameters["width"] == 1920
    assert request.parameters["height"] == 1080
    assert request.parameters["fps"] == 24
