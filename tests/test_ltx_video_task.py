from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

import ai_video_factory.gpu.ltx_video as ltx_video
from ai_video_factory.gpu.contracts import GPUJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.gpu.ltx_video import (
    LTX_GENERATION_PROFILE,
    DirectLTX25Backend,
    LTXModelFiles,
    LTXVideoParameters,
    LTXVideoTaskRunner,
    ltx_num_frames_for_duration,
)
from ai_video_factory.gpu.tasks import CopyTaskRunner, TaskRunnerRegistry


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
        keyframe_path: Path,
        output_path: Path,
        parameters: LTXVideoParameters,
    ) -> None:
        self.calls.append(
            {
                "keyframe_path": keyframe_path,
                "output_path": output_path,
                "parameters": parameters,
            }
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-mp4")


def parameters(**updates: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "generation_profile": LTX_GENERATION_PROFILE,
        "prompt": "A deliberate samurai motion shot.",
        "seed": 43,
        "width": 1280,
        "height": 720,
        "fps": 24,
        "num_frames": 121,
    }
    values.update(updates)
    return values


def request(**parameter_updates: Any) -> GPUJobRequest:
    job_id = "phase8-shot-001-deadbeef"
    return GPUJobRequest(
        job_id=job_id,
        task="video.ltx25.generate",
        inputs=[
            ObjectInput(
                name="keyframe",
                key="phase8/keyframes/shot-001.png",
                sha256="a" * 64,
                content_type="image/png",
            )
        ],
        output=ObjectOutput(
            key=f"jobs/{job_id}/shot_001.mp4",
            content_type="video/mp4",
        ),
        parameters=parameters(**parameter_updates),
    )


def test_ltx_num_frames_rounds_up_to_temporal_grid() -> None:
    durations = [3.5, 7.38, 5.06, 2.82, 9.68, 6.6, 7.38, 2.58]
    expected = [89, 185, 129, 73, 233, 161, 185, 65]

    resolved = [ltx_num_frames_for_duration(value, fps=24) for value in durations]

    assert resolved == expected
    for duration, num_frames in zip(durations, resolved, strict=True):
        assert (num_frames - 1) % 8 == 0
        assert num_frames / 24 >= duration


def test_ltx_num_frames_rejects_invalid_duration_or_fps() -> None:
    with pytest.raises(ValueError, match="positive finite"):
        ltx_num_frames_for_duration(0)
    with pytest.raises(ValueError, match="positive finite"):
        ltx_num_frames_for_duration(float("inf"))
    with pytest.raises(ValueError, match="fps must be positive"):
        ltx_num_frames_for_duration(3.0, fps=0)


def test_ltx_parameters_enforce_generation_profile_and_shape() -> None:
    validated = LTXVideoParameters.model_validate(parameters())
    assert validated.generation_profile == LTX_GENERATION_PROFILE
    assert validated.width == 1280
    assert validated.height == 720
    assert validated.num_frames == 121

    with pytest.raises(ValidationError, match="generation_profile"):
        LTXVideoParameters.model_validate(parameters(generation_profile="other-profile"))
    with pytest.raises(ValidationError, match="divisible by 64"):
        LTXVideoParameters.model_validate(parameters(width=770))
    with pytest.raises(ValidationError, match=r"8k \+ 1"):
        LTXVideoParameters.model_validate(parameters(num_frames=120))
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LTXVideoParameters.model_validate(parameters(steps=20))


def test_task_runner_lifecycle_delegates_to_backend() -> None:
    backend = FakeBackend()
    runner = LTXVideoTaskRunner(backend=backend)

    runner.prepare()
    runner.ready()

    assert backend.prepare_calls == 1
    assert backend.ready_calls == 1


def test_task_runner_generates_one_silent_ready_mp4_contract(tmp_path: Path) -> None:
    backend = FakeBackend()
    runner = LTXVideoTaskRunner(backend=backend)
    keyframe = tmp_path / "keyframe.png"
    keyframe.write_bytes(b"png")

    artifact = runner.run(
        request(),
        {"keyframe": keyframe},
        tmp_path / "work",
    )

    assert artifact.content_type == "video/mp4"
    assert artifact.path.read_bytes() == b"fake-mp4"
    assert len(backend.calls) == 1
    assert backend.calls[0]["parameters"].prompt == "A deliberate samurai motion shot."
    assert backend.calls[0]["parameters"].seed == 43


def test_task_runner_rejects_wrong_input_or_output_contract(tmp_path: Path) -> None:
    backend = FakeBackend()
    runner = LTXVideoTaskRunner(backend=backend)
    keyframe = tmp_path / "keyframe.png"
    keyframe.write_bytes(b"png")

    with pytest.raises(ValueError, match="exactly one 'keyframe'"):
        runner.run(request(), {"other": keyframe}, tmp_path / "work-a")

    valid = request()
    wrong_output = valid.model_copy(
        update={
            "output": ObjectOutput(
                key=f"jobs/{valid.job_id}/shot_001.bin",
                content_type="application/octet-stream",
            )
        }
    )
    with pytest.raises(ValueError, match="content_type"):
        runner.run(wrong_output, {"keyframe": keyframe}, tmp_path / "work-b")

    assert backend.calls == []


def test_phase8_registry_keeps_smoke_and_runs_lifecycle_hooks() -> None:
    backend = FakeBackend()
    video_runner = LTXVideoTaskRunner(backend=backend)
    registry = TaskRunnerRegistry.phase8(video_runner)

    registry.prepare()
    registry.ready()

    assert isinstance(registry.get("infrastructure.copy"), CopyTaskRunner)
    assert registry.get("video.ltx25.generate") is video_runner
    assert backend.prepare_calls == 1
    assert backend.ready_calls == 1


def _seed_model_files(root: Path) -> None:
    for path in LTXModelFiles.from_root(root).paths():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")


def test_direct_backend_prepare_caches_pipeline_and_discards_generated_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_root = tmp_path / "models"
    _seed_model_files(model_root)
    keyframe = tmp_path / "keyframe.png"
    Image.new("RGB", (1280, 720), (32, 64, 96)).save(keyframe, format="PNG")

    state: dict[str, Any] = {
        "pipeline_builds": 0,
        "pipeline_calls": [],
        "encodes": [],
        "conditionings": [],
    }

    class FakeVideo:
        shape = (1, 3, 121, 768, 1280)

        def __getitem__(self, item: Any) -> str:
            state["video_crop"] = item
            return "cropped-video"

    class FakePipeline:
        def __init__(self, **kwargs: Any) -> None:
            state["pipeline_builds"] += 1
            state["pipeline_init"] = kwargs

        def __call__(self, **kwargs: Any) -> Any:
            state["pipeline_calls"].append(kwargs)
            return SimpleNamespace(video=FakeVideo(), num_frames=121, tiling_config="tiling")

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

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

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

    bindings = ltx_video._LTXBindings(
        torch=FakeTorch,
        distilled_pipeline=FakePipeline,
        model_paths=FakeModelPaths,
        quantization_kind=FakeQuantizationKind,
        offload_mode=FakeOffloadMode,
        image_conditioning_input=fake_conditioning,
        encode_video=fake_encode_video,
        get_video_chunks_number=lambda num_frames, tiling: 3,
    )
    monkeypatch.setattr(ltx_video, "_load_ltx_bindings", lambda: bindings)

    backend = DirectLTX25Backend(model_root=model_root)
    backend.prepare()
    backend.ready()
    backend.prepare()

    validated = LTXVideoParameters.model_validate(parameters())
    backend.generate(
        keyframe_path=keyframe,
        output_path=tmp_path / "first.mp4",
        parameters=validated,
    )
    backend.generate(
        keyframe_path=keyframe,
        output_path=tmp_path / "second.mp4",
        parameters=validated,
    )

    assert backend.pipeline_loaded is True
    assert state["pipeline_builds"] == 1
    assert len(state["pipeline_calls"]) == 2
    assert state["pipeline_init"]["quantization"] == "fp8-policy"
    assert state["pipeline_init"]["offload_mode"] == "cpu"
    assert state["pipeline_init"]["device"] == "device:cuda"
    assert state["conditionings"][0]["frame_idx"] == 0
    assert state["conditionings"][0]["strength"] == 1.0
    assert state["pipeline_calls"][0]["width"] == 1280
    assert state["pipeline_calls"][0]["height"] == 768
    assert state["encodes"][0]["video"] == "cropped-video"
    spatial_crop = state["video_crop"][-2:]
    assert spatial_crop == (slice(24, 744), slice(0, 1280))
    assert len(state["encodes"]) == 2
    assert all(call["audio"] is None for call in state["encodes"])


def test_direct_backend_rejects_missing_cuda_before_pipeline_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_root = tmp_path / "models"
    _seed_model_files(model_root)

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return False

    class FakeTorch:
        cuda = FakeCuda()

    bindings = SimpleNamespace(torch=FakeTorch)
    monkeypatch.setattr(ltx_video, "_load_ltx_bindings", lambda: bindings)

    backend = DirectLTX25Backend(model_root=model_root)
    with pytest.raises(RuntimeError, match="CUDA is not available"):
        backend.prepare()
