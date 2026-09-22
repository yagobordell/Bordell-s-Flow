from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.workers.ltx25.a2v import (
    LTX_A2V_DEFAULT_PROMPT,
    LTX_A2V_GENERATION_PROFILE,
    LTX_A2V_TASK,
    A2VGenerationResult,
    AudioProbe,
    LTXA2VModelFiles,
    LTXA2VParameters,
    LTXA2VTaskRunner,
    VideoProbe,
    ltx_a2v_hard_max_seconds,
    probe_audio,
)
from ai_video_factory.workers.ltx25.jobs import ltx_a2v_application_job_id


class FakeA2VBackend:
    def prepare(self) -> None:
        pass

    def ready(self) -> None:
        pass

    def generate(self, **kwargs):
        output = kwargs["output_path"]
        output.write_bytes(b"mp4")
        return A2VGenerationResult(
            num_frames=121,
            effective_audio_duration_seconds=5.0416666667,
            output_video_duration_seconds=5.042,
            model_load_seconds=4.0,
            inference_seconds=30.0,
            encode_seconds=2.0,
            total_elapsed_seconds=36.0,
            peak_vram_bytes=123456789,
            input_audio=AudioProbe(
                duration_seconds=5.0,
                codec="pcm_s16le",
                sample_rate=48000,
                channels=1,
            ),
            output_video=VideoProbe(
                duration_seconds=5.042,
                width=1280,
                height=720,
                fps=24.0,
                has_audio=True,
            ),
        )


def _request(tmp_path: Path) -> InferenceJobRequest:
    return InferenceJobRequest(
        job_id="ltx-a2v-segment-001",
        task=LTX_A2V_TASK,
        inputs=[
            ObjectInput(
                name="avatar_image",
                key="inputs/avatar.png",
                content_type="image/png",
            ),
            ObjectInput(
                name="audio",
                key="inputs/segment.wav",
                content_type="audio/wav",
            ),
        ],
        output=ObjectOutput(
            key="jobs/ltx-a2v-segment-001/video.mp4",
            content_type="video/mp4",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key="jobs/ltx-a2v-segment-001/metadata.json",
                content_type="application/json",
            )
        },
        parameters=LTXA2VParameters().model_dump(mode="json"),
    )


def test_a2v_parameters_keep_audio_as_duration_driver() -> None:
    parameters = LTXA2VParameters()
    assert parameters.generation_profile == LTX_A2V_GENERATION_PROFILE
    assert parameters.prompt == LTX_A2V_DEFAULT_PROMPT
    assert parameters.width == 1280
    assert parameters.height == 720
    assert parameters.fps == 24
    assert "num_frames" not in parameters.model_dump()


def test_a2v_rejects_noncanonical_off_grid_shape() -> None:
    with pytest.raises(ValueError, match="divisible by 64"):
        LTXA2VParameters(width=1000, height=700)


def test_a2v_model_pack_adds_dev_transformer_and_distilled_lora(tmp_path: Path) -> None:
    files = LTXA2VModelFiles.from_root(tmp_path)
    assert files.transformer.name == "ltx-2.5-22b-dev-transformer-bf16.safetensors"
    assert files.distilled_lora.name == "ltx-2.5-22b-distilled-lora-450-bf16.safetensors"
    assert files.audio_vae.name == "ltx-2.5-audio-vae-bf16.safetensors"


def test_a2v_runner_requires_image_audio_and_writes_metadata(tmp_path: Path) -> None:
    avatar = tmp_path / "avatar.png"
    Image.new("RGB", (1280, 720)).save(avatar)
    audio = tmp_path / "segment.wav"
    audio.write_bytes(b"audio")
    request = _request(tmp_path)
    runner = LTXA2VTaskRunner(backend=FakeA2VBackend())

    artifact = runner.run(
        request,
        {"avatar_image": avatar, "audio": audio},
        tmp_path,
    )

    assert artifact.content_type == "video/mp4"
    assert set(artifact.sidecars) == {"metadata"}
    metadata = json.loads(artifact.sidecars["metadata"].path.read_text(encoding="utf-8"))
    assert metadata["generation_mode"] == "audio_to_video"
    assert metadata["input_audio_duration_seconds"] == 5.0
    assert metadata["effective_audio_duration_seconds"] == pytest.approx(5.0416666667)
    assert metadata["output_video_duration_seconds"] == 5.042
    assert metadata["num_frames"] == 121
    assert metadata["width"] == 1280
    assert metadata["height"] == 720
    assert metadata["quantization"] == "fp8-cast"
    assert metadata["offload_mode"] == "cpu"
    assert metadata["peak_vram_bytes"] == 123456789
    assert metadata["total_elapsed_seconds"] == 36.0
    assert len(metadata["image_sha256"]) == 64
    assert len(metadata["audio_sha256"]) == 64


def test_a2v_runner_rejects_missing_audio(tmp_path: Path) -> None:
    request = _request(tmp_path)
    runner = LTXA2VTaskRunner(backend=FakeA2VBackend())
    avatar = tmp_path / "avatar.png"
    Image.new("RGB", (1280, 720)).save(avatar)

    with pytest.raises(ValueError, match="avatar_image.*audio"):
        runner.run(request, {"avatar_image": avatar}, tmp_path)


def test_a2v_job_id_is_content_addressed_and_segment_scoped() -> None:
    kwargs = {
        "segment_id": "avatar-shot-003",
        "prompt": LTX_A2V_DEFAULT_PROMPT,
        "avatar_image_sha256": "a" * 64,
        "audio_sha256": "b" * 64,
        "seed": 10,
        "width": 1280,
        "height": 720,
        "fps": 24,
    }
    first = ltx_a2v_application_job_id(**kwargs)
    second = ltx_a2v_application_job_id(**kwargs)
    changed = ltx_a2v_application_job_id(**{**kwargs, "audio_sha256": "c" * 64})

    assert first == second
    assert first != changed
    assert first.startswith("ltx-a2v-avatar-shot-003-")



def test_a2v_hard_limit_matches_official_1024_raw_frame_clamp() -> None:
    assert ltx_a2v_hard_max_seconds(fps=24) == pytest.approx(1024 / 24)


def test_a2v_audio_probe_rejects_duration_beyond_upstream_clamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "long.wav"
    audio.write_bytes(b"placeholder")
    monkeypatch.setattr(
        "ai_video_factory.workers.ltx25.a2v._probe_json",
        lambda _path: {
            "format": {"duration": "43.0"},
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "pcm_s16le",
                    "sample_rate": "48000",
                    "channels": 1,
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="1024-frame"):
        probe_audio(audio, fps=24)
