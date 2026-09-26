from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import ai_video_factory.workers.ltx25.a2v as a2v
from ai_video_factory.workers.ltx25 import LTXA2VModelFiles


@dataclass(frozen=True)
class FakeVideoGuiderParams:
    cfg_scale: float = 3.0
    stg_scale: float = 1.0
    modality_scale: float = 3.0
    rescale_scale: float = 0.7
    stg_blocks: list[int] = field(default_factory=lambda: [28])


def _seed_model_files(root: Path) -> None:
    files = LTXA2VModelFiles.from_root(root)
    for path in (*files.shared.paths(), files.dev_transformer, files.distilled_lora):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")


def make_a2v_bindings(state: dict[str, Any]) -> a2v._A2VBindings:
    class FakeInferenceMode:
        def __enter__(self) -> None:
            state["inference_entries"] += 1
            state["inference_depth"] += 1

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            state["inference_depth"] -= 1

    class FakeChunk:
        shape = (89, 768, 1280, 3)

        def __getitem__(self, item: Any) -> str:
            state.setdefault("crops", []).append(item)
            return "cropped"

    class FakePipeline:
        def __init__(self, **kwargs: Any) -> None:
            assert state["inference_depth"] > 0
            state["builds"] += 1
            state["pipeline_init"] = kwargs
            state.setdefault("pipeline_inits", []).append(kwargs)

        def __call__(self, **kwargs: Any) -> Any:
            assert state["inference_depth"] > 0
            state["calls"].append(kwargs)
            state["tiling_budget"] = FakeTilingHelpers.activation_budget_bytes()
            num_frames = int(kwargs.get("num_frames") or 89)
            audio_samples = int(num_frames * 24000 / 24)
            return SimpleNamespace(
                video=iter([FakeChunk()]),
                audio=SimpleNamespace(
                    waveform=SimpleNamespace(shape=(1, audio_samples)),
                    sampling_rate=24000,
                ),
                num_frames=num_frames,
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
        DISK = "disk"

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

        @staticmethod
        def current_device() -> int:
            return 0

        @staticmethod
        def mem_get_info(index: int) -> tuple[int, int]:
            del index
            return (24_000_000_000, 32_000_000_000)

        @staticmethod
        def memory_allocated(index: int) -> int:
            del index
            return 2_000_000_000

        @staticmethod
        def memory_reserved(index: int) -> int:
            del index
            return 4_000_000_000

        @staticmethod
        def get_per_process_memory_fraction(index: int) -> float:
            del index
            return 1.0

    class FakeDevice:
        def __init__(self, value: str) -> None:
            self.value = value
            self.index = 0

        def __str__(self) -> str:
            return f"device:{self.value}"

    class FakeTorch:
        cuda = FakeCuda()

        @staticmethod
        def device(value: str) -> FakeDevice:
            return FakeDevice(value)

        @staticmethod
        def inference_mode() -> FakeInferenceMode:
            return FakeInferenceMode()

    def fake_conditioning(**kwargs: Any) -> dict[str, Any]:
        state["conditionings"].append(kwargs)
        return kwargs

    def fake_encode_video(**kwargs: Any) -> None:
        assert state["inference_depth"] > 0
        state["encodes"].append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"encoded")

    class FakeTilingHelpers:
        @staticmethod
        def activation_budget_bytes(device: Any = None) -> int:
            del device
            return 20_000_000_000

    def fake_cleanup(device: Any = None) -> None:
        state.setdefault("cleanup_devices", []).append(str(device))

    state["original_guider"] = FakeVideoGuiderParams()
    bindings = a2v._A2VBindings(
        torch=FakeTorch,
        a2v_pipeline=FakePipeline,
        reference_a2v_pipeline=FakePipeline,
        model_paths=FakeModelPaths,
        quantization_kind=FakeQuantizationKind,
        offload_mode=FakeOffloadMode,
        image_conditioning_input=fake_conditioning,
        encode_video=fake_encode_video,
        get_video_chunks_number=lambda num_frames, tiling: 2,
        diffvae_apply=FakeDiffvaeApply,
        tiling_helpers=FakeTilingHelpers,
        cleanup_accelerator_memory=fake_cleanup,
        lora_tuple=lambda path, strength, sd_ops: (path, strength, sd_ops),
        lora_sd_ops="rename-map",
        detect_params=lambda _: SimpleNamespace(
            num_inference_steps=30,
            video_guider_params=state["original_guider"],
        ),
        distilled_sigmas=(1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0),
        stage_2_sigmas=(0.909375, 0.725, 0.421875, 0.0),
        default_negative_prompt="negative",
    )
    return bindings
