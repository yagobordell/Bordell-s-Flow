from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

import ai_video_factory.workers.ltx25.a2v as a2v
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.errors import NonRetryableTaskError
from ai_video_factory.workers.ltx25 import (
    LTX_A2V_GENERATION_PROFILE,
    LTX_A2V_TASK,
    DirectLTX25AudioToVideoBackend,
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

    def prepare(self) -> None:
        self.prepare_calls += 1

    def ready(self) -> None:
        self.ready_calls += 1

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


def _request(**updates: Any) -> InferenceJobRequest:
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
        parameters=parameters,
    )


def _seed_model_files(root: Path) -> None:
    files = LTXA2VModelFiles.from_root(root)
    for path in (*files.shared.paths(), files.dev_transformer, files.distilled_lora):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")


def test_a2v_parameters_are_audio_driven_and_keep_720p24_defaults() -> None:
    parameters = LTXAudioToVideoParameters()

    assert parameters.generation_profile == LTX_A2V_GENERATION_PROFILE
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
    assert first.startswith("ltx-a2v-003-")


def test_a2v_model_files_require_dev_transformer_and_distilled_lora(tmp_path: Path) -> None:
    _seed_model_files(tmp_path)
    files = LTXA2VModelFiles.from_root(tmp_path)
    files.validate()

    files.dev_transformer.unlink()
    with pytest.raises(FileNotFoundError, match="dev-transformer"):
        files.validate()


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
    }

    class FakeChunk:
        shape = (89, 768, 1280, 3)

        def __getitem__(self, item: Any) -> str:
            state.setdefault("crops", []).append(item)
            return "cropped"

    class FakePipeline:
        def __init__(self, **kwargs: Any) -> None:
            state["builds"] += 1
            state["pipeline_init"] = kwargs

        def __call__(self, **kwargs: Any) -> Any:
            state["calls"].append(kwargs)
            return SimpleNamespace(
                video=iter([FakeChunk()]),
                audio=SimpleNamespace(
                    waveform=SimpleNamespace(shape=(1, 89000)),
                    sampling_rate=24000,
                ),
                num_frames=89,
                tiling_config="tiling",
            )

    class FakeModelPaths:
        @classmethod
        def from_split(cls, **kwargs: Any) -> dict[str, Any]:
            state["model_paths"] = kwargs
            return kwargs

    class FakeFP8Cast:
        def to_policy(self, *, checkpoint_path: str) -> str:
            state["quantization_checkpoint"] = checkpoint_path
            return "fp8-policy"

    class FakeQuantizationKind:
        FP8_CAST = FakeFP8Cast()

    class FakeOffloadMode:
        CPU = "cpu"

    class FakeDiffvaeApply:
        @staticmethod
        def natten_available() -> bool:
            return True

        @staticmethod
        def triton_na_available() -> bool:
            return True

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def reset_peak_memory_stats() -> None:
            state["reset_peak"] = True

        @staticmethod
        def max_memory_allocated() -> int:
            return 123456

        @staticmethod
        def empty_cache() -> None:
            state["empty_cache"] = True

    class FakeTorch:
        cuda = FakeCuda()

        @staticmethod
        def device(value: str) -> str:
            return f"device:{value}"

    def fake_conditioning(**kwargs: Any) -> dict[str, Any]:
        state["conditionings"].append(kwargs)
        return kwargs

    def fake_encode_video(**kwargs: Any) -> None:
        state["encodes"].append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"encoded")

    bindings = a2v._A2VBindings(
        torch=FakeTorch,
        a2v_pipeline=FakePipeline,
        model_paths=FakeModelPaths,
        quantization_kind=FakeQuantizationKind,
        offload_mode=FakeOffloadMode,
        image_conditioning_input=fake_conditioning,
        encode_video=fake_encode_video,
        get_video_chunks_number=lambda num_frames, tiling: 2,
        diffvae_apply=FakeDiffvaeApply,
        lora_tuple=lambda path, strength, sd_ops: (path, strength, sd_ops),
        lora_sd_ops="rename-map",
        detect_params=lambda _: SimpleNamespace(
            num_inference_steps=30,
            video_guider_params="official-guider",
        ),
        default_negative_prompt="negative",
    )
    monkeypatch.setattr(a2v, "_load_a2v_bindings", lambda: bindings)
    monkeypatch.setattr(
        a2v,
        "probe_audio",
        lambda _: a2v.AudioProbe("pcm_s16le", 24000, 1, 4.0),
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
    assert "dev-transformer" in state["model_paths"]["transformer_path"]
    assert state["calls"][0]["num_frames"] is None
    assert state["calls"][0]["num_inference_steps"] == 30
    assert state["calls"][0]["video_guider_params"] == "official-guider"
    assert state["calls"][0]["height"] == 768
    assert state["encodes"][0]["audio"] is not None
    assert list(state["encodes"][0]["video"]) == ["cropped"]
    assert metadata["input_audio_duration_seconds"] == 4.0
    assert metadata["effective_audio_duration_seconds"] == pytest.approx(89000 / 24000)
    assert metadata["num_frames"] == 89
    assert metadata["peak_vram_bytes"] == 123456
    assert metadata["pipeline_reused"] is False
    assert state["conditionings"][0]["strength"] == 1.0
