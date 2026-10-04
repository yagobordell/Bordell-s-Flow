"""CPU quality gates: zero-math prompt reuse and strict Eager provenance."""

from __future__ import annotations

import ast
import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@dataclass(frozen=True)
class FakeSample:
    prompt: str
    seed: int
    image: str
    audio_path: str


def _prompt_helper():
    # Pinned LTX is GPU-only and intentionally absent on free CI runners.
    # Extract the actual production helper AST, not a rewritten test clone.
    path = (Path(__file__).resolve().parents[3]
            / "src/ai_video_factory/workers/ltx25/paired_a2v.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_prepare_independent_prompt_contexts"
    )
    module = ast.Module(body=[fn], type_ignores=[])
    scope = {
        "A2VSample": FakeSample, "Any": object, "time": time,
        "LTX_A2V_STATIC_NEGATIVE_PROMPT": "UNCHANGED STATIC NEGATIVE PROMPT",
    }
    exec(compile(module, str(path), "exec"), scope)
    return scope[fn.name]


def test_identical_prompts_reuse_exact_context_objects_without_changing_slots() -> None:
    encode = Mock(side_effect=lambda texts, **_kwargs: (
        SimpleNamespace(video_encoding=["posv"], audio_encoding=["posa"]),
        SimpleNamespace(video_encoding=["negv"], audio_encoding=["nega"]),
    ))
    pipeline = SimpleNamespace(
        image_conditioner=SimpleNamespace(resolve_crf=lambda images: images),
        prompt_encoder=encode,
    )
    samples = (
        FakeSample("EXACT prompt", 11, "image-a", "audio-a"),
        FakeSample("EXACT prompt", 12, "image-b", "audio-b"),
    )
    rows, facts = _prompt_helper()(pipeline, samples)
    assert encode.call_count == 1
    assert facts["same_pair_context_reuse"] is True
    assert facts["prompt_encoder_calls"] == 1
    assert rows[0][1] is rows[1][1]
    assert rows[0][2] is rows[1][2]
    assert rows[0][0].seed == 11 and rows[1][0].seed == 12
    assert rows[0][0].audio_path != rows[1][0].audio_path
    assert encode.call_args.args == (
        ["EXACT prompt", "UNCHANGED STATIC NEGATIVE PROMPT"],
    )
    assert encode.call_args.kwargs == {
        "enhance_first_prompt": False, "enhance_static_cache": False,
    }


def test_different_prompts_do_not_share_gemma_or_embeddings() -> None:
    def unique(texts, **_kwargs):
        return (
            SimpleNamespace(video_encoding=object(), audio_encoding=object()),
            SimpleNamespace(video_encoding=object(), audio_encoding=object()),
        )
    encoder = Mock(side_effect=unique)
    pipeline = SimpleNamespace(
        image_conditioner=SimpleNamespace(resolve_crf=lambda images: images),
        prompt_encoder=encoder,
    )
    rows, facts = _prompt_helper()(
        pipeline,
        (FakeSample("one", 5, "image", "audio1"),
         FakeSample("two", 6, "image", "audio2")),
    )
    assert encoder.call_count == 2
    assert facts["same_pair_context_reuse"] is False
    assert rows[0][1] is not rows[1][1]
    assert rows[0][2] is not rows[1][2]


def test_triton_reference_must_match_all_original_quality_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[3] / "scripts/smoke")
    )
    smoke = importlib.import_module("scripts.smoke.submit_ltx25_a2v_batch2_smoke")
    from ai_video_factory.workers.ltx25.a2v import LTX_A2V_RUN_STATIC_FAST_PROFILE

    def build_baseline():
        a = tmp_path / "batch2-a.mp4"
        b = tmp_path / "batch2-b.mp4"
        a.write_bytes(b"eager-a")
        b.write_bytes(b"eager-b")
        source = {
            "images": ["sha-img-a", "sha-img-b"],
            "prompts": ["prompt a", "prompt b"],
            "seeds": [4, 5],
            "num_frames": 137,
            "profile": LTX_A2V_RUN_STATIC_FAST_PROFILE,
            "geometry": [1920, 1080, 24],
            "audio": [
                {"sha256": "sha-a", "sample_rate": 44100,
                 "sample_count": 12345, "num_frames": 137},
                {"sha256": "sha-b", "sample_rate": 44100,
                 "sample_count": 12345, "num_frames": 137},
            ],
        }
        request = {
            "task": smoke.LTX_A2V_BATCH2_TASK,
            "job_id": "approved-eager-pair",
            "parameters": {
                "a": {
                    "prompt": "prompt a", "seed": 4, "width": 1920,
                    "height": 1080, "fps": 24,
                    "generation_profile": LTX_A2V_RUN_STATIC_FAST_PROFILE,
                },
                "b": {
                    "prompt": "prompt b", "seed": 5, "width": 1920,
                    "height": 1080, "fps": 24,
                    "generation_profile": LTX_A2V_RUN_STATIC_FAST_PROFILE,
                },
                "vae_backend": "eager", "accept_full_hd_oom_risk": True,
            },
        }
        report = {
            "mode": "batch2", "actual_gpu_batch_size": 2,
            "video_vae_backend": "eager",
            "audio_tail_validation": "PASSED_BOTH_OUTPUTS",
            "technical_validation": {
                label: {"width": 1920, "height": 1080, "fps": 24,
                        "num_frames": 137}
                for label in ("a", "b")
            },
        }
        for filename, value in (
            ("job-request.json", request),
            ("reference-provenance.json", source),
            ("experiment-summary.json", report),
        ):
            (tmp_path / filename).write_text(json.dumps(value))
        return a, b, source

    a, b, source = build_baseline()
    accepted = smoke._require_matching_eager_baseline(a, b, source)
    assert accepted["baseline_job_id"] == "approved-eager-pair"
    assert len(accepted["baseline_video_sha256"]) == 2
    with pytest.raises(ValueError, match="prompts"):
        smoke._require_matching_eager_baseline(
            a, b, {**source, "prompts": ["changed", "prompt b"]},
        )
    with pytest.raises(ValueError, match="audio source"):
        smoke._require_matching_eager_baseline(
            a, b, {**source, "audio": [
                {**source["audio"][0], "sha256": "different"},
                source["audio"][1],
            ]},
        )
    report_path = tmp_path / "experiment-summary.json"
    report = json.loads(report_path.read_text())
    report["video_vae_backend"] = "triton"
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="fully verified Eager"):
        smoke._require_matching_eager_baseline(a, b, source)


def test_strict_gpu_probe_never_runs_in_the_main_process() -> None:
    from ai_video_factory.workers.ltx25 import triton_a2v_probe

    with patch.object(
        triton_a2v_probe.subprocess, "run",
        return_value=SimpleNamespace(
            returncode=1, stderr="kernel failure", stdout="",
        ),
    ) as child:
        with pytest.raises(RuntimeError, match="FAILED"):
            triton_a2v_probe.ensure_triton_gpu_probe()
    argv = child.call_args.args[0]
    assert argv[1:3] == ["-m", triton_a2v_probe.__name__]
    assert argv[-1] == "--gpu-probe"
