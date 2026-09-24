from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _ltx_a2v_support import _seed_model_files
from PIL import Image, ImageChops, ImageOps
from pydantic import ValidationError

import ai_video_factory.workers.ltx25.a2v as a2v
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.errors import NonRetryableTaskError
from ai_video_factory.inference.gpu_failures import DEFAULT_GPU_MAX_ATTEMPTS
from ai_video_factory.workers.ltx25 import (
    LTX_A2V_DEV_GENERATION_PROFILE,
    LTX_A2V_GENERATION_PROFILE,
    LTX_A2V_TASK,
    LTXA2VModelFiles,
    LTXAudioToVideoParameters,
    LTXAudioToVideoTaskRunner,
    ltx_a2v_application_job_id,
)


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.prepare_calls = 0
        self.ready_calls = 0
        self.invalidate_calls = 0

    def prepare(self) -> None:
        self.prepare_calls += 1

    def ready(self) -> None:
        self.ready_calls += 1

    def invalidate_pipeline(self) -> None:
        self.invalidate_calls += 1

    def generate(
        self,
        *,
        image_path: Path,
        audio_path: Path,
        output_path: Path,
        parameters: LTXAudioToVideoParameters,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "image_path": image_path,
                "audio_path": audio_path,
                "output_path": output_path,
                "parameters": parameters,
            }
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-mp4")
        return {
            "generation_mode": "audio_to_video",
            "input_audio_duration_seconds": 4.0,
            "effective_audio_duration_seconds": 3.708,
            "output_video_duration_seconds": 3.708,
            "num_frames": 89,
            "fps": 24,
            "width": 1280,
            "height": 720,
            "seed": parameters.seed,
        }


def _request(
    *, max_attempts: int | None = DEFAULT_GPU_MAX_ATTEMPTS, **updates: Any
) -> InferenceJobRequest:
    parameters: dict[str, Any] = {
        "generation_profile": LTX_A2V_GENERATION_PROFILE,
        "prompt": "A stable talking head.",
        "seed": 73,
        "width": 1280,
        "height": 720,
        "fps": 24,
    }
    parameters.update(updates)
    job_id = "ltx-a2v-segment-003-deadbeef"
    return InferenceJobRequest(
        job_id=job_id,
        task=LTX_A2V_TASK,
        inputs=[
            ObjectInput(
                name="image",
                key="avatar/reference.png",
                sha256="a" * 64,
                content_type="image/png",
            ),
            ObjectInput(
                name="audio",
                key="audio/segment-003.wav",
                sha256="b" * 64,
                content_type="audio/wav",
            ),
        ],
        output=ObjectOutput(
            key=f"jobs/{job_id}/avatar_segment_003.mp4",
            content_type="video/mp4",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=f"jobs/{job_id}/metadata.json",
                content_type="application/json",
            )
        },
        max_attempts=max_attempts,
        parameters=parameters,
    )


def test_a2v_parameters_are_audio_driven_and_keep_720p24_defaults() -> None:
    parameters = LTXAudioToVideoParameters()

    assert parameters.generation_profile == LTX_A2V_GENERATION_PROFILE
    assert "distilled" in parameters.generation_profile
    assert LTXAudioToVideoParameters(
        generation_profile=LTX_A2V_DEV_GENERATION_PROFILE
    ).generation_profile == LTX_A2V_DEV_GENERATION_PROFILE
    with pytest.raises(ValidationError, match="generation_profile"):
        LTXAudioToVideoParameters(generation_profile="ltx25-a2v-unknown")
    assert parameters.width == 1280
    assert parameters.height == 720
    assert parameters.fps == 24
    assert "synchronization" in parameters.prompt

    with pytest.raises(ValidationError, match="extra_forbidden"):
        LTXAudioToVideoParameters.model_validate(
            {"num_frames": 121}
        )


def test_a2v_runner_requires_image_audio_and_metadata_sidecar(tmp_path: Path) -> None:
    backend = FakeBackend()
    runner = LTXAudioToVideoTaskRunner(backend=backend)
    image = tmp_path / "avatar.png"
    audio = tmp_path / "speech.wav"
    image.write_bytes(b"png")
    audio.write_bytes(b"wav")

    artifact = runner.run(
        _request(),
        {"image": image, "audio": audio},
        tmp_path / "work",
    )

    assert artifact.content_type == "video/mp4"
    assert artifact.path.read_bytes() == b"fake-mp4"
    assert len(artifact.sidecars) == 1
    assert artifact.sidecars[0].name == "metadata"
    metadata = artifact.sidecars[0].path.read_text(encoding="utf-8")
    assert '"generation_mode": "audio_to_video"' in metadata
    assert backend.calls[0]["parameters"].seed == 73

    with pytest.raises(NonRetryableTaskError, match="image.*audio"):
        runner.run(_request(), {"image": image}, tmp_path / "bad")

    with pytest.raises(
        NonRetryableTaskError,
        match=rf"max_attempts={DEFAULT_GPU_MAX_ATTEMPTS}",
    ):
        runner.run(
            _request(max_attempts=2),
            {"image": image, "audio": audio},
            tmp_path / "invalid-retry-budget",
        )


def test_a2v_runner_invalidates_pipeline_after_retryable_gpu_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    runner = LTXAudioToVideoTaskRunner(backend=backend)
    image = tmp_path / "avatar.png"
    audio = tmp_path / "speech.wav"
    image.write_bytes(b"png")
    audio.write_bytes(b"wav")

    def fail_generation(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("CUDA out of memory while executing A2V")

    monkeypatch.setattr(backend, "generate", fail_generation)

    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        runner.run(
            _request(),
            {"image": image, "audio": audio},
            tmp_path / "retryable-gpu-failure",
        )

    assert backend.invalidate_calls == 1


def test_a2v_runner_keeps_pipeline_for_non_gpu_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    runner = LTXAudioToVideoTaskRunner(backend=backend)
    image = tmp_path / "avatar.png"
    audio = tmp_path / "speech.wav"
    image.write_bytes(b"png")
    audio.write_bytes(b"wav")

    def fail_generation(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("unexpected media encoder failure")

    monkeypatch.setattr(backend, "generate", fail_generation)

    with pytest.raises(RuntimeError, match="media encoder failure"):
        runner.run(
            _request(),
            {"image": image, "audio": audio},
            tmp_path / "non-gpu-failure",
        )

    assert backend.invalidate_calls == 0


def test_a2v_job_id_fingerprints_image_audio_and_segment() -> None:
    first = ltx_a2v_application_job_id(
        segment_id="003",
        prompt="talk",
        image_sha256="a" * 64,
        audio_sha256="b" * 64,
        seed=42,
        width=1280,
        height=720,
        fps=24,
    )
    same = ltx_a2v_application_job_id(
        segment_id="003",
        prompt="talk",
        image_sha256="a" * 64,
        audio_sha256="b" * 64,
        seed=42,
        width=1280,
        height=720,
        fps=24,
    )
    changed = ltx_a2v_application_job_id(
        segment_id="003",
        prompt="talk",
        image_sha256="a" * 64,
        audio_sha256="c" * 64,
        seed=42,
        width=1280,
        height=720,
        fps=24,
    )

    assert first == same
    assert first != changed
    dev = ltx_a2v_application_job_id(
        segment_id="003",
        prompt="talk",
        image_sha256="a" * 64,
        audio_sha256="b" * 64,
        seed=42,
        width=1280,
        height=720,
        fps=24,
        generation_profile=LTX_A2V_DEV_GENERATION_PROFILE,
    )
    assert first != dev
    assert first.startswith("ltx-a2v-003-")


def test_a2v_model_files_require_dev_transformer_and_distilled_lora(tmp_path: Path) -> None:
    _seed_model_files(tmp_path)
    files = LTXA2VModelFiles.from_root(tmp_path)
    files.validate()

    files.dev_transformer.unlink()
    with pytest.raises(FileNotFoundError, match="dev-transformer"):
        files.validate()


def test_a2v_avatar_preserves_qwen_image_edges(tmp_path: Path) -> None:
    source = tmp_path / "avatar.png"
    image = Image.new("RGB", (1280, 736), (48, 48, 48))
    image.paste((255, 0, 0), (0, 0, 30, 736))
    image.paste((0, 0, 255), (1250, 0, 1280, 736))
    image.paste((0, 255, 0), (30, 0, 1250, 6))
    image.paste((255, 255, 0), (30, 730, 1250, 736))
    image.save(source, format="PNG")

    adapted = a2v._prepare_avatar_image(
        source,
        tmp_path / "avatar-grid.png",
        requested_width=1280,
        requested_height=720,
        pipeline_width=1280,
        pipeline_height=768,
    )
    with Image.open(adapted) as result:
        assert result.size == (1280, 768)
        assert result.getpixel((5, 384)) == (255, 0, 0)
        assert result.getpixel((1275, 384)) == (0, 0, 255)
        assert result.getpixel((640, 0)) == (0, 255, 0)
        assert result.getpixel((640, 767)) == (255, 255, 0)
        assert result.getpixel((640, 384)) == (48, 48, 48)


@pytest.mark.parametrize(
    ("source_size", "output_size", "grid_size"),
    [
        ((720, 1280), (1280, 720), (1280, 768)),
        ((1920, 800), (1280, 720), (1280, 768)),
        ((1536, 864), (1280, 720), (1280, 768)),
        ((1280, 736), (768, 1280), (768, 1280)),
    ],
)
def test_a2v_noncanonical_avatar_retains_center_crop(
    tmp_path: Path,
    source_size: tuple[int, int],
    output_size: tuple[int, int],
    grid_size: tuple[int, int],
) -> None:
    source = tmp_path / "source.png"
    width, height = source_size
    image = Image.new("RGB", source_size, (48, 48, 48))
    image.paste((255, 0, 0), (0, 0, width // 4, height))
    image.paste((0, 0, 255), (3 * width // 4, 0, width, height))
    image.paste((0, 255, 0), (0, 0, width, height // 4))
    image.paste((255, 255, 0), (0, 3 * height // 4, width, height))
    image.save(source, format="PNG")

    adapted = a2v._prepare_avatar_image(
        source,
        tmp_path / "adapted.png",
        requested_width=output_size[0],
        requested_height=output_size[1],
        pipeline_width=grid_size[0],
        pipeline_height=grid_size[1],
    )
    expected = ImageOps.fit(
        image,
        output_size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    pad_top = (grid_size[1] - output_size[1]) // 2
    with Image.open(adapted) as result:
        assert result.size == grid_size
        actual = result.crop(
            (0, pad_top, output_size[0], pad_top + output_size[1])
        )
        assert ImageChops.difference(actual, expected).getbbox() is None


def test_a2v_mono_audio_is_upmixed_to_stereo_without_duration_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "speech.wav"
    source.write_bytes(b"mono")
    destination = tmp_path / "prepared" / "speech-stereo.wav"
    mono_probe = a2v.AudioProbe(
        codec="pcm_s16le",
        sample_rate=24000,
        channels=1,
        duration_seconds=5.0,
    )
    calls: list[list[str]] = []

    monkeypatch.setattr(a2v.shutil, "which", lambda _: "ffmpeg")

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        assert kwargs["check"] is True
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        calls.append(command)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"stereo")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(a2v.subprocess, "run", fake_run)
    monkeypatch.setattr(
        a2v,
        "probe_audio",
        lambda path: a2v.AudioProbe(
            codec="pcm_s16le",
            sample_rate=24000,
            channels=2,
            duration_seconds=5.0,
        )
        if path == destination
        else mono_probe,
    )

    prepared_path, prepared_probe = a2v._prepare_pipeline_audio(
        source,
        destination,
        probe=mono_probe,
    )

    assert prepared_path == destination
    assert prepared_probe.channels == 2
    assert prepared_probe.duration_seconds == 5.0
    assert len(calls) == 1
    assert calls[0][calls[0].index("-ac") + 1] == "2"
    assert calls[0][calls[0].index("-ar") + 1] == "24000"


def test_a2v_stereo_audio_passes_through_without_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "speech-stereo.wav"
    source.write_bytes(b"stereo")
    stereo_probe = a2v.AudioProbe(
        codec="pcm_s16le",
        sample_rate=24000,
        channels=2,
        duration_seconds=5.0,
    )

    monkeypatch.setattr(
        a2v.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("ffmpeg should not run for stereo audio"),
    )

    prepared_path, prepared_probe = a2v._prepare_pipeline_audio(
        source,
        tmp_path / "unused.wav",
        probe=stereo_probe,
    )

    assert prepared_path == source
    assert prepared_probe is stereo_probe
