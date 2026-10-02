"""Offline quality and safety gates for explicit fast LTX 2.5 I2V profiles."""

from __future__ import annotations

import ast
import json
import logging
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.workers.ltx25 import fast_delivery
from ai_video_factory.workers.ltx25 import model as ltx_model
from ai_video_factory.workers.ltx25.fast_delivery import (
    FAST_DELIVERY_SIZE,
    FAST_INTERNAL_SIZE,
    deliver_fast_video,
    prepare_fast_keyframe,
)
from ai_video_factory.workers.ltx25.model import (
    LTX_FAST_LOWRES_NO_OFFLOAD_PROFILE,
    LTX_FAST_LOWRES_PROFILE,
    LTX_FAST_LOWRES_TRITON_PROFILE,
    LTX_FAST_PROFILES,
    LTX_FAST_VANILLA_PROFILE,
    LTX_FAST_VANILLA_REFERENCE_PROFILE,
    LTX_GENERATION_PROFILE,
    DirectLTX25Backend,
    LTXVideoParameters,
)
from ai_video_factory.workflows.video_generation import build_video_generation_plan


def _params(profile: str) -> LTXVideoParameters:
    return LTXVideoParameters(
        generation_profile=profile,
        prompt="The subject moves beside a fixed camera.",
        seed=1042, width=1280, height=720, fps=24, num_frames=121,
    )


def test_fast_profiles_have_immutable_distinct_replay_identities(tmp_path: Path) -> None:
    image = tmp_path / "frame.png"
    Image.new("RGB", (1280, 720)).save(image)
    inputs = (
        [StoryboardKeyframe(shot_id=1, uri=image.name)],
        [VideoPrompt(shot_id=1, prompt="A subject stirs a drink silently.")],
        [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=5.0)],
    )
    assert {
        LTX_FAST_VANILLA_REFERENCE_PROFILE,
        LTX_FAST_VANILLA_PROFILE,
        LTX_FAST_LOWRES_TRITON_PROFILE,
        LTX_FAST_LOWRES_NO_OFFLOAD_PROFILE,
    } <= set(LTX_FAST_PROFILES)
    plans = [
        build_video_generation_plan(
            *inputs, keyframe_base_dir=tmp_path, seed_base=4242,
            generation_profile=p,
        )[0]
        for p in (LTX_GENERATION_PROFILE, *LTX_FAST_PROFILES)
    ]
    assert len({p.request.job_id for p in plans}) == len(plans)
    assert len({p.request.fingerprint() for p in plans}) == len(plans)
    assert len({p.request.output.key for p in plans}) == len(plans)
    assert all(p.request.parameters["seed"] == 4243 for p in plans)
    assert all(p.request.parameters["num_frames"] == 121 for p in plans)
    assert all(p.request.parameters["width"] == 1280 for p in plans)
    assert all(p.request.parameters["height"] == 720 for p in plans)


def test_pinned_fast_generator_is_true_vanilla_joint_two_stage() -> None:
    """AST guard: no camera CFG or modified sigmas/ancestral seed in FAST."""
    source = Path(
        "src/ai_video_factory/workers/ltx25/fast_i2v.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    generator = next(
        x for x in tree.body
        if isinstance(x, ast.FunctionDef) and x.name == "generate_fast_distilled"
    )
    calls = [ast.unparse(x.func) for x in ast.walk(generator) if isinstance(x, ast.Call)]
    assert calls.count("SimpleDenoiser") == 2
    assert calls.count("GuidedDenoiser") == 0
    assert "[prompt]" in source
    assert "negative_prompt" not in source
    assert "self._stage_1_sampler_kwargs(seed)" in source
    assert "audio=ModalitySpec(context=audio_context)" in source
    assert "initial_latent=audio_state.latent" in source
    assert "if decode_audio:" in source
    assert "decoded_video = self.video_decoder(" in source
    assert "decode_audio: bool = False" in source
    assert "stage_1_sigmas: torch.Tensor = DISTILLED_SIGMAS" in source
    assert "stage_2_sigmas: torch.Tensor = STAGE_2_DISTILLED_SIGMAS" in source


def test_fast_keyframe_preserves_qwen_full_field_of_view(tmp_path: Path) -> None:
    image = Image.new("RGB", (1280, 736), (22, 35, 48))
    image.paste((252, 2, 2), (0, 0, 100, 100))
    image.paste((2, 2, 252), (1180, 635, 1280, 736))
    source = tmp_path / "qwen.png"
    target = tmp_path / "out" / "fast-keyframe.png"
    image.save(source)
    result = prepare_fast_keyframe(source, target)
    assert result == target
    with Image.open(result) as fitted:
        assert fitted.size == FAST_INTERNAL_SIZE
        # Both original corner regions survive fitting; never center-crop 736.
        assert fitted.getpixel((10, 10)) == (252, 2, 2)
        assert fitted.getpixel((1010, 560)) == (2, 2, 252)


def test_fast_keyframe_rejects_arbitrary_aspect_ratio(tmp_path: Path) -> None:
    source = tmp_path / "wrong.png"
    Image.new("RGB", (1000, 1000)).save(source)
    with pytest.raises(ValueError, match="16:9"):
        prepare_fast_keyframe(source, tmp_path / "out.png")


def test_fast_delivery_scales_without_crop_and_publishes_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"faked raw source")
    output = tmp_path / "delivery" / "final.mp4"
    captured: list[str] = []

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        captured.extend(args)
        assert kwargs["timeout"] == 300
        assert args[args.index("-map") + 1] == "0:v:0"
        assert args[args.index("-vf") + 1] == (
            "scale=1280:720:flags=lanczos,setsar=1"
        )
        assert "-an" in args
        assert "-crop" not in " ".join(args)
        Path(args[-1]).write_bytes(b"fake completed delivery")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(fast_delivery.subprocess, "run", fake_run)
    assert deliver_fast_video(raw, output) >= 0.0
    assert output.read_bytes() == b"fake completed delivery"
    assert raw.read_bytes() == b"faked raw source"
    assert FAST_DELIVERY_SIZE == (1280, 720)
    assert captured[0] == "ffmpeg"


def test_fast_delivery_error_preserves_raw_and_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"raw")
    output = tmp_path / "final.mp4"
    monkeypatch.setattr(
        fast_delivery.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="codec error"),
    )
    with pytest.raises(RuntimeError, match="raw 576p retained"):
        deliver_fast_video(raw, output)
    assert raw.read_bytes() == b"raw"
    assert not output.exists()
    assert not output.with_name("final.delivery.tmp.mp4").exists()


def test_fast_backend_preserves_public_contract_and_reports_per_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    keyframe = tmp_path / "original.png"
    Image.new("RGB", (1280, 720)).save(keyframe)
    backend = DirectLTX25Backend(model_root=tmp_path, device="cpu")
    pipeline = object()
    backend._pipeline = pipeline
    backend._pipeline_requested_diffvae_backend = "eager"
    backend._pipeline_effective_diffvae_backend = "eager"
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False),
        inference_mode=nullcontext,
    )
    generation_args: list[dict[str, object]] = []
    written_paths: list[Path] = []

    def fake_gen(p: object, **kwargs: object) -> SimpleNamespace:
        assert p is pipeline
        generation_args.append(kwargs)
        return SimpleNamespace(
            video=iter([b"raw-frame-stream"]),
            num_frames=kwargs["num_frames"],
            tiling_config=object(),
        )

    def fake_encode(**kwargs: object) -> None:
        assert kwargs["audio"] is None
        raw = Path(str(kwargs["output_path"]))
        written_paths.append(raw)
        raw.write_bytes(b"".join(kwargs["video"]))

    # Do not import the GPU-only LTX upstream packages during offline CI.
    monkeypatch.setitem(
        sys.modules,
        "ai_video_factory.workers.ltx25.fast_i2v",
        SimpleNamespace(generate_fast_distilled=fake_gen),
    )
    monkeypatch.setattr(
        fast_delivery, "deliver_fast_video",
        lambda raw, output: output.write_bytes(raw.read_bytes()) or 0.0,
    )
    bindings = SimpleNamespace(
        torch=fake_torch, image_conditioning_input=lambda **kw: kw,
        encode_video=fake_encode, get_video_chunks_number=lambda *args: 1,
    )
    monkeypatch.setattr(backend, "_get_bindings", lambda: bindings)
    monkeypatch.setattr(backend, "_validate_runtime", lambda _: None)
    monkeypatch.setattr(
        backend, "_get_or_build_pipeline",
        lambda _bindings, **kwargs: pipeline,
    )
    with caplog.at_level(logging.INFO):
        backend.generate(
            keyframe_path=keyframe, output_path=tmp_path / "out.mp4",
            parameters=_params(LTX_FAST_LOWRES_PROFILE),
        )
    assert (tmp_path / "out.mp4").read_bytes() == b"raw-frame-stream"
    assert written_paths[0].name == "out.raw-576p.mp4"
    assert not written_paths[0].exists()
    assert generation_args[0]["prompt"] == _params(LTX_FAST_LOWRES_PROFILE).prompt
    assert generation_args[0]["seed"] == 1042
    assert generation_args[0]["width"] == 1024
    assert generation_args[0]["height"] == 576
    assert generation_args[0]["num_frames"] == 121
    assert generation_args[0]["decode_audio"] is False
    assert "negative_prompt" not in generation_args[0]
    logs = [
        json.loads(x.getMessage().split("LTX25_I2V_PHASE_METRICS ")[1])
        for x in caplog.records if "LTX25_I2V_PHASE_METRICS " in x.getMessage()
    ]
    assert len(logs) == 1
    assert logs[0]["stage1_expected_forward_calls"] == 8
    assert logs[0]["vanilla_simple_denoiser"] is True
    assert logs[0]["internal_width"] == 1024
    assert logs[0]["internal_height"] == 576
    assert logs[0]["delivery_width"] == 1280
    assert logs[0]["delivery_height"] == 720


def test_pipeline_offload_switch_rebuilds_instead_of_reusing_invalid_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = DirectLTX25Backend(model_root=tmp_path, device="cpu")
    creations: list[dict[str, object]] = []

    def builder(**kwargs: object) -> object:
        creations.append(kwargs)
        return object()

    bindings = SimpleNamespace(
        torch=SimpleNamespace(device=lambda x: x),
        model_paths=SimpleNamespace(from_split=lambda **kwargs: object()),
        quantization_kind=SimpleNamespace(
            FP8_CAST=SimpleNamespace(to_policy=lambda checkpoint_path: object())
        ),
        offload_mode=SimpleNamespace(CPU="cpu", NONE="none"),
        distilled_pipeline=builder,
    )
    monkeypatch.setattr(
        ltx_model, "_select_diffvae_backend",
        lambda bindings, requested: requested,
    )
    first = backend._get_or_build_pipeline(bindings)
    assert backend._get_or_build_pipeline(bindings) is first
    other = backend._get_or_build_pipeline(bindings, requested_offload_mode="none")
    assert other is not first
    assert [x["offload_mode"] for x in creations] == ["cpu", "none"]


def test_fast_benchmark_requires_explicit_opt_in_for_no_offload() -> None:
    script = Path("scripts/smoke/run_ltx25_i2v_fast_benchmark.ps1").read_text(
        encoding="utf-8"
    )
    assert '$ProfileSelection -contains "fast-lowres-none" -or' in script
    assert '$ProfileSelection -contains "fast-vanilla-none") -and -not $AllowNoOffload)' in script
    assert "baseline" in script.lower()
    assert "Assert-SameInputs" in script
    assert 'if (-not $?) { throw "I2V cleanup FAILED' in script
    if pwsh := shutil.which("pwsh"):
        result = subprocess.run(
            [
                pwsh, "-NoProfile", "-NonInteractive", "-File",
                "scripts/smoke/run_ltx25_i2v_fast_benchmark.ps1",
                "-Image", "unused.png", "-Prompt", "any",
                "-ExpectedPinnedImage", "repo@sha256:" + "a" * 64,
                "-ProfileSelection", "fast-lowres-none",
            ],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0
        assert "pass -AllowNoOffload" in result.stdout + result.stderr



@pytest.mark.parametrize("same_sha", [True, False])
def test_fast_batch_fails_closed_on_real_vanilla_sha_gate(
    tmp_path: Path, same_sha: bool
) -> None:
    """Run the actual guard fragment with local receipts; never invoke Salad."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell not installed")
    source = Path("scripts/smoke/run_ltx25_i2v_fast_benchmark.ps1").read_text(
        encoding="utf-8"
    )
    guard = '            if ($Profile -eq "fast-vanilla" -and'
    assert source.count(guard) == 1
    block = guard + source.split(guard, maxsplit=1)[1].split(
        '            $Result.status = "video_valid_metrics_pending"', maxsplit=1
    )[0]
    reference = tmp_path / "r1-fast-vanilla-reference"
    reference.mkdir()
    sha = "a" * 64
    (reference / "i2v-smoke-report.json").write_text(
        json.dumps({"video_sha256": sha}), encoding="utf-8"
    )
    current = tmp_path / "r1-fast-vanilla"
    current.mkdir()
    actual = sha if same_sha else "b" * 64
    root = str(tmp_path).replace("'", "''")
    ps = (
        "$ErrorActionPreference = 'Stop'\\n"
        f"$BatchPath = '{root}'\\n"
        "$Profile = 'fast-vanilla'\\n"
        "$Profiles = @('fast-vanilla-reference', 'fast-vanilla')\\n"
        "$Round = 1\\n"
        "$RoundSeedBase = 4242\\n"
        "$ProfilePath = Join-Path $BatchPath 'r1-fast-vanilla'\\n"
        f"$Result = [ordered]@{{video_sha256 = '{actual}'}}\\n"
        "function Save-BatchJson { param([string]$Path, $Value) "
        "$Value | ConvertTo-Json -Depth 6 | "
        "Set-Content -LiteralPath $Path -Encoding utf8 }\\n"
        "try {\\n"
        + block + "\\n"
        + "} catch { Write-Output $_.Exception.Message; exit 23 }\\n"
        + "exit 0\\n"
    )
    # Use actual line breaks: the fixture above stays obvious in Python.
    ps = ps.replace("\\n", "\n")
    completed = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, check=False,
    )
    assert (completed.returncode == 0) is same_sha, (
        completed.stdout + completed.stderr
    )
    receipt = json.loads(
        (current / "vanilla-equivalence.json").read_text(encoding="utf-8-sig")
    )
    assert receipt["exact_mp4_sha256_match"] is same_sha
    assert receipt["round"] == 1
    if not same_sha:
        assert "VANILLA QUALITY GATE FAILED" in completed.stdout
