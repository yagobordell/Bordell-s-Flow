from pathlib import Path

from PIL import Image

from ai_video_factory.providers.salad_ltx25 import build_ltx_a2v_request
from ai_video_factory.workers.ltx25 import LTX_A2V_TASK


def test_build_ltx_a2v_request_is_ready_for_future_orchestration(tmp_path: Path) -> None:
    avatar = tmp_path / "avatar.png"
    Image.new("RGB", (1280, 720)).save(avatar)
    audio = tmp_path / "segment.wav"
    audio.write_bytes(b"fixture-audio")

    request, image_key, audio_key = build_ltx_a2v_request(
        segment_id="avatar-shot-003",
        avatar_image=avatar,
        audio_segment=audio,
        prompt="Stable talking head.",
        seed=77,
    )

    assert request.task == LTX_A2V_TASK
    assert {item.name for item in request.inputs} == {"avatar_image", "audio"}
    assert request.output.key.endswith("/video.mp4")
    assert request.sidecar_outputs is not None
    assert request.sidecar_outputs["metadata"].key.endswith("/metadata.json")
    assert request.parameters["width"] == 1280
    assert request.parameters["height"] == 720
    assert request.parameters["fps"] == 24
    assert "num_frames" not in request.parameters
    assert image_key.startswith("ltx25-a2v/inputs/images/")
    assert audio_key.startswith("ltx25-a2v/inputs/audio/")
