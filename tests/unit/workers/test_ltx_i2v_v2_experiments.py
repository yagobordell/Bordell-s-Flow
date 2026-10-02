"""Offline fail-closed contracts for full-resolution fast I2V optimization profiles."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.workers.ltx25 import model as ltx
from ai_video_factory.workers.ltx25.fast_prompt_metrics import profile_prompt_encoder
from ai_video_factory.workers.ltx25.i2v_metrics import I2VPhaseMetrics
from ai_video_factory.workers.ltx25.nvfp4_runtime import require_nvfp4_runtime
from ai_video_factory.workflows.video_generation import build_video_generation_plan


@pytest.mark.parametrize(
    "profile",
    [
        ltx.LTX_FAST_FULLRES_NO_OFFLOAD_PROFILE,
        ltx.LTX_FAST_FULLRES_TRITON_PROFILE,
        ltx.LTX_FAST_FULLRES_NVFP4_PROFILE,
    ],
)
def test_every_new_profile_is_valid_opt_in_and_full_resolution(profile: str) -> None:
    parsed = ltx.LTXVideoParameters(
        generation_profile=profile,
        prompt="A woman gestures without speaking at the camera.",
        seed=77,
        width=1280, height=720, fps=24, num_frames=121,
    )
    assert parsed.generation_profile == profile
    assert profile in ltx.LTX_FAST_PROFILES
    assert profile not in ltx.LTX_FAST_LOWRES_PROFILES
    assert profile != ltx.LTX_GENERATION_PROFILE


def test_new_profiles_have_independent_postgres_and_r2_identities(tmp_path: Path) -> None:
    png = tmp_path / "original.png"
    Image.new("RGB", (1280, 720)).save(png)
    plan_args = (
        [StoryboardKeyframe(shot_id=1, uri=png.name)],
        [VideoPrompt(shot_id=1, prompt="The subject raises her hands.")],
        [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=5.0)],
    )
    profiles = (
        ltx.LTX_GENERATION_PROFILE,
        ltx.LTX_FAST_VANILLA_PROFILE,
        ltx.LTX_FAST_FULLRES_NO_OFFLOAD_PROFILE,
        ltx.LTX_FAST_FULLRES_TRITON_PROFILE,
        ltx.LTX_FAST_FULLRES_NVFP4_PROFILE,
    )
    planned = [
        build_video_generation_plan(
            *plan_args, generation_profile=p, seed_base=4242,
            keyframe_base_dir=tmp_path,
        )[0].request for p in profiles
    ]
    assert len({p.job_id for p in planned}) == len(profiles)
    assert len({p.output.key for p in planned}) == len(profiles)
    assert len({p.fingerprint() for p in planned}) == len(profiles)
    assert all(p.parameters["num_frames"] == 121 for p in planned)
    assert all(p.parameters["width"] == 1280 for p in planned)


def _backend_bindings(constructed: list[dict[str, object]]) -> SimpleNamespace:
    def build(**kwargs: object) -> object:
        constructed.append(kwargs)
        return object()

    return SimpleNamespace(
        torch=SimpleNamespace(device=lambda device: device),
        model_paths=SimpleNamespace(from_split=lambda **kwargs: kwargs),
        quantization_kind=SimpleNamespace(
            FP8_CAST=SimpleNamespace(
                to_policy=lambda checkpoint_path: ("fp8", checkpoint_path)
            ),
            NVFP4_PREQUANT=SimpleNamespace(
                to_policy=lambda checkpoint_path: ("nvfp4", checkpoint_path)
            ),
        ),
        offload_mode=SimpleNamespace(CPU="cpu", NONE="none"),
        distilled_pipeline=build,
    )


def test_fullres_fp8_no_offload_is_explicit_and_does_not_poison_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed: list[dict[str, object]] = []
    backend = ltx.DirectLTX25Backend(model_root=tmp_path, device="cpu")
    monkeypatch.setattr(
        ltx, "_select_diffvae_backend", lambda bindings, requested: requested
    )
    bindings = _backend_bindings(constructed)
    default = backend._get_or_build_pipeline(bindings)
    assert constructed[-1]["offload_mode"] == "cpu"
    assert constructed[-1]["quantization"][0] == "fp8"
    resident = backend._get_or_build_pipeline(
        bindings, requested_offload_mode="none"
    )
    assert resident is not default
    assert constructed[-1]["offload_mode"] == "none"
    assert constructed[-1]["quantization"][0] == "fp8"
    assert backend._get_or_build_pipeline(
        bindings, requested_offload_mode="none"
    ) is resident
    restored = backend._get_or_build_pipeline(bindings)
    assert restored is not resident
    assert constructed[-1]["offload_mode"] == "cpu"


def test_nvfp4_requires_own_docker_environment(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LTX_I2V_NVFP4_RUNTIME", raising=False)
    fake = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
    with pytest.raises(RuntimeError, match="separate pinned Dockerfile"):
        require_nvfp4_runtime(fake, "cuda")


def test_nvfp4_requires_blackwell_and_compiled_kernel(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LTX_I2V_NVFP4_RUNTIME", "enabled")
    cuda = SimpleNamespace(
        is_available=lambda: True,
        get_device_capability=lambda device: (9, 0),
    )
    fake = SimpleNamespace(cuda=cuda, device=lambda x: x)
    with pytest.raises(RuntimeError, match="Blackwell"):
        require_nvfp4_runtime(fake, "cuda")
    cuda.get_device_capability = lambda device: (12, 0)
    monkeypatch.setitem(
        sys.modules, "ltx_kernels",
        SimpleNamespace(nvfp4=SimpleNamespace(unavailable_reason=lambda: "no extension")),
    )
    with pytest.raises(RuntimeError, match="no extension"):
        require_nvfp4_runtime(fake, "cuda")
    sys.modules["ltx_kernels"].nvfp4.unavailable_reason = lambda: None
    require_nvfp4_runtime(fake, "cuda")


def test_nvfp4_requires_offload_none_and_real_checkpoint_before_instantiation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_video_factory.workers.ltx25 import nvfp4_runtime

    constructed: list[dict[str, object]] = []
    bindings = _backend_bindings(constructed)
    backend = ltx.DirectLTX25Backend(model_root=tmp_path, device="cuda")
    monkeypatch.setattr(
        ltx, "_select_diffvae_backend", lambda bindings, requested: requested
    )
    checked: list[str] = []
    monkeypatch.setattr(
        nvfp4_runtime, "require_nvfp4_runtime",
        lambda torch_module, device: checked.append(device),
    )
    with pytest.raises(ValueError, match="nonstreaming"):
        backend._get_or_build_pipeline(
            bindings, requested_quantization="nvfp4_prequant"
        )
    with pytest.raises(FileNotFoundError, match="Opt-in NVFP4 checkpoint"):
        backend._get_or_build_pipeline(
            bindings,
            requested_offload_mode="none",
            requested_quantization="nvfp4_prequant",
        )
    assert checked == ["cuda"]
    assert not constructed
    model_file = (
        tmp_path / "diffusion_models" /
        "ltx-2.5-22b-distilled-transformer-nvfp4.safetensors"
    )
    model_file.parent.mkdir()
    model_file.write_bytes(b"fake stub (never loaded in offline test)")
    nvfp4_pipeline = backend._get_or_build_pipeline(
        bindings,
        requested_offload_mode="none",
        requested_quantization="nvfp4_prequant",
    )
    assert constructed[-1]["quantization"] == ("nvfp4", str(model_file))
    assert constructed[-1]["offload_mode"] == "none"
    assert backend._get_or_build_pipeline(
        bindings,
        requested_offload_mode="none",
        requested_quantization="nvfp4_prequant",
    ) is nvfp4_pipeline
    previous = backend._get_or_build_pipeline(
        bindings, requested_offload_mode="none",
    )
    assert previous is not nvfp4_pipeline
    assert constructed[-1]["quantization"][0] == "fp8"


def test_opt_in_prompt_instrumentation_restores_methods_on_failure() -> None:
    class FakePrompt:
        @contextmanager
        def _text_encoder_ctx(self):
            yield "fake-gemma"

        def _build_embeddings_processor(self):
            return "fake-embeddings"

        def __call__(self):
            with self._text_encoder_ctx() as text:
                assert text == "fake-gemma"
            assert self._build_embeddings_processor() == "fake-embeddings"
            raise RuntimeError("synthetic failure")

    prompt = FakePrompt()
    original_text = prompt._text_encoder_ctx
    original_embeddings = prompt._build_embeddings_processor
    metrics = I2VPhaseMetrics(
        SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)), "cpu"
    )
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with profile_prompt_encoder(prompt, metrics):
            prompt()
    assert prompt._text_encoder_ctx is original_text
    assert prompt._build_embeddings_processor is original_embeddings
    assert metrics.timings["prompt_text_encoder_load_and_encode_seconds"] >= 0
    assert metrics.timings["prompt_embeddings_processor_build_seconds"] >= 0


def test_native_image_and_asset_opt_in_cannot_modify_normal_salad_bootstrap() -> None:
    docker = Path("docker/workers/ltx25/Dockerfile").read_text(encoding="utf-8")
    nvfp4 = Path("docker/workers/ltx25/Dockerfile.nvfp4").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(
        Path("docker/workers/ltx25/model-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    download = Path("docker/workers/ltx25/download_models.sh").read_text(
        encoding="utf-8"
    )
    assert "LTX_I2V_NVFP4_RUNTIME=enabled" not in docker
    assert "LTX_INCLUDE_I2V_NVFP4_ASSETS=true" not in docker
    assert "2.11.0-cuda12.8-cudnn9-devel" in nvfp4
    assert "LTX_I2V_NVFP4_RUNTIME=enabled" in nvfp4
    assert "LTX_INCLUDE_I2V_NVFP4_ASSETS=true" in nvfp4
    assert "TORCH_CUDA_ARCH_LIST=12.0 MAX_JOBS=2" in nvfp4
    assert manifest["revision"] == "6c7e5e573ac1667efc83407806fe9b0b93730e60"
    assert manifest["i2v_nvfp4_files"] == [
        "diffusion_models/ltx-2.5-22b-distilled-transformer-nvfp4.safetensors"
    ]
    assert "if include_nvfp4:" in download
    assert "INCLUDE_I2V_NVFP4_ASSETS=" in download


def test_batch_and_collector_only_enable_native_profile_explicitly() -> None:
    benchmark = Path(
        "scripts/smoke/run_ltx25_i2v_fast_benchmark.ps1"
    ).read_text(encoding="utf-8")
    collector = Path(
        "scripts/smoke/ltx25_i2v_benchmark_metrics.py"
    ).read_text(encoding="utf-8")
    assert '"fast-vanilla-none"' in benchmark
    assert '"fast-vanilla-triton"' in benchmark
    assert '"fast-vanilla-nvfp4"' in benchmark
    assert 'if (-not $AllowNvfp4)' in benchmark
    assert "Official pinned NVFP4 asset unavailable." in benchmark
    assert '"fast-vanilla-nvfp4": LTX_FAST_FULLRES_NVFP4_PROFILE' in collector
    assert "seed_identity = $EffectiveSeedIdentity" in benchmark



def test_official_hf_nvfp4_metadata_preflight_is_pinned_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import runpy

    scope = runpy.run_path("scripts/smoke/check_ltx25_nvfp4_source.py")
    check = scope["require_official_nvfp4_available"]
    manifest = json.loads(
        Path("docker/workers/ltx25/model-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    seen: list[str] = []

    class FakeResponse:
        def __enter__(self) -> io.BytesIO:
            return io.BytesIO(
                json.dumps(
                    [{
                        "path": manifest["i2v_nvfp4_files"][0],
                        "size": 123456,
                    }]
                ).encode()
            )

        def __exit__(self, *args: object) -> None:
            pass

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        assert timeout == 30
        seen.append(str(request.full_url))
        assert request.get_header("Authorization") == "Bearer test-token"
        return FakeResponse()

    monkeypatch.setitem(check.__globals__, "urlopen", fake_urlopen)
    assert check(manifest, "test-token") == 123456
    assert manifest["revision"] in seen[0]
    assert seen[0].endswith("/diffusion_models")
    with pytest.raises(ValueError, match="official"):
        check({**manifest, "repository": "unverified/model"}, "test-token")
    with pytest.raises(ValueError, match="pinned"):
        check({**manifest, "revision": "main"}, "test-token")
    with pytest.raises(ValueError, match="official distilled"):
        check({**manifest, "i2v_nvfp4_files": ["random"]}, "test-token")

    def empty_listing(request: object, timeout: int) -> FakeResponse:
        class Empty(FakeResponse):
            def __enter__(self) -> io.BytesIO:
                return io.BytesIO(b"[]")

        return Empty()

    monkeypatch.setitem(check.__globals__, "urlopen", empty_listing)
    with pytest.raises(RuntimeError, match="missing at pinned"):
        check(manifest, "test-token")
