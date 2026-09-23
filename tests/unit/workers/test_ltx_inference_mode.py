from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

import ai_video_factory.workers.ltx25.model as ltx_video
from ai_video_factory.workers.ltx25.model import (
    LTX_GENERATION_PROFILE,
    DirectLTX25Backend,
    LTXModelFiles,
    LTXVideoParameters,
)


def _seed_model_files(root: Path) -> None:
    for path in LTXModelFiles.from_root(root).paths():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")


def test_direct_backend_runs_pipeline_and_encode_in_inference_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_root = tmp_path / "models"
    _seed_model_files(model_root)
    keyframe = tmp_path / "keyframe.png"
    Image.new("RGB", (1536, 864)).save(keyframe, format="PNG")
    state = {"depth": 0, "entries": 0, "pipeline_calls": 0, "encodes": 0}

    class FakeInferenceMode:
        def __enter__(self) -> None:
            state["entries"] += 1
            state["depth"] += 1

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            state["depth"] -= 1

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

    class FakeTorch:
        cuda = FakeCuda()

        @staticmethod
        def device(value: str) -> str:
            return f"device:{value}"

        @staticmethod
        def inference_mode() -> FakeInferenceMode:
            return FakeInferenceMode()

    class FakeModelPaths:
        @classmethod
        def from_split(cls, **kwargs: Any) -> dict[str, Any]:
            return kwargs

    class FakeFP8Cast:
        def to_policy(self, *, checkpoint_path: str) -> str:
            return f"fp8:{checkpoint_path}"

    class FakeQuantizationKind:
        FP8_CAST = FakeFP8Cast()

    class FakeOffloadMode:
        CPU = "cpu"

    class FakeDiffVAEApply:
        natten_available = staticmethod(lambda: True)
        triton_na_available = staticmethod(lambda: True)

    class FakePipeline:
        def __init__(self, **kwargs: Any) -> None:
            assert state["depth"] > 0

        def __call__(self, **kwargs: Any) -> Any:
            assert state["depth"] > 0
            state["pipeline_calls"] += 1
            return SimpleNamespace(video="video", num_frames=89, tiling_config="tiling")

    def fake_encode_video(**kwargs: Any) -> None:
        assert state["depth"] > 0
        state["encodes"] += 1
        Path(kwargs["output_path"]).write_bytes(b"mp4")

    bindings = ltx_video._LTXBindings(
        torch=FakeTorch,
        distilled_pipeline=FakePipeline,
        model_paths=FakeModelPaths,
        quantization_kind=FakeQuantizationKind,
        offload_mode=FakeOffloadMode,
        image_conditioning_input=lambda **kwargs: kwargs,
        encode_video=fake_encode_video,
        get_video_chunks_number=lambda num_frames, tiling: 1,
        diffvae_apply=FakeDiffVAEApply,
    )
    monkeypatch.setattr(ltx_video, "_load_ltx_bindings", lambda: bindings)

    backend = DirectLTX25Backend(model_root=model_root)
    backend.prepare()
    backend.generate(
        keyframe_path=keyframe,
        output_path=tmp_path / "output.mp4",
        parameters=LTXVideoParameters(
            generation_profile=LTX_GENERATION_PROFILE,
            prompt="A cinematic motion shot.",
            seed=43,
            width=1280,
            height=720,
            fps=24,
            num_frames=89,
        ),
    )

    assert state["entries"] == 2
    assert state["depth"] == 0
    assert state["pipeline_calls"] == 1
    assert state["encodes"] == 1
    assert FakeDiffVAEApply.natten_available() is False
    assert FakeDiffVAEApply.triton_na_available() is False
