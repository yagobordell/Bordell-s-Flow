"""Experimental A2V jobs are never claimed by the production worker or vice versa."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.inference.errors import NonRetryableTaskError
from ai_video_factory.workers.ltx25.a2v import (
    LTX_A2V_PRODUCTION_FAST_PROFILE,
    LTX_A2V_BENCH_NONE_PROFILE,
    LTX_A2V_EXPERIMENTAL_TASK,
    LTX_A2V_TASK,
    LTXAudioToVideoTaskRunner,
)
from ai_video_factory.workers.ltx25.settings import LTX25WorkerSettings
from ai_video_factory.workers.ltx25.worker_factory import build_ltx25_runners
from scripts.smoke.check_ltx_a2v_benchmark_jobs import _A2V_TASK as GATED_TASK


def _local_settings(mode: str) -> LTX25WorkerSettings:
    return LTX25WorkerSettings.model_validate(
        {
            "worker_mode": "local",
            "task_mode": mode,
            "worker_poll_jobs": True,
        }
    )


def test_production_and_experimental_registries_cannot_cross_claim() -> None:
    production = build_ltx25_runners(_local_settings("a2v"))
    experiment = build_ltx25_runners(_local_settings("a2v_experimental"))
    i2v = build_ltx25_runners(_local_settings("i2v"))
    assert production.task_names == (LTX_A2V_TASK,)
    assert experiment.task_names == (LTX_A2V_EXPERIMENTAL_TASK,)
    assert GATED_TASK == LTX_A2V_EXPERIMENTAL_TASK
    assert not set(production.task_names) & set(experiment.task_names)
    assert not set(i2v.task_names) & set(experiment.task_names)


def test_experimental_runner_rejects_production_task_and_profile() -> None:
    runner = LTXAudioToVideoTaskRunner(backend=SimpleNamespace(), experimental=True)
    request = SimpleNamespace(
        task=LTX_A2V_TASK,
        parameters={"generation_profile": LTX_A2V_BENCH_NONE_PROFILE},
    )
    with pytest.raises(NonRetryableTaskError, match="cannot execute task"):
        runner._validate_request(request, {})
    request.task = LTX_A2V_EXPERIMENTAL_TASK
    request.parameters = {"generation_profile": "unrelated-production-profile"}
    with pytest.raises(NonRetryableTaskError, match="experimental profile"):
        runner._validate_request(request, {})


def test_salad_experiment_is_only_opt_in_task_mode() -> None:
    cfg = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    prod = cfg["services"]["ltx25"]
    exp = cfg["services"]["ltx25_a2v_experimental"]
    assert prod["environment"]["LTX_WORKER_TASK_MODE"] == "a2v"
    assert exp["environment"]["LTX_WORKER_TASK_MODE"] == "a2v_experimental"
    assert exp["group_name"] != prod["group_name"]
    assert "ltx25_a2v_experimental" not in cfg["stack"]["service_order"]


def test_structural_streaming_failure_is_terminal_only_for_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def broken_generate(**_kwargs: object) -> None:
        calls.append(True)
        raise TypeError("cannot unpack non-iterable FeedForward object")

    backend = SimpleNamespace(generate=broken_generate)
    request = SimpleNamespace(
        task=LTX_A2V_EXPERIMENTAL_TASK,
        parameters={"generation_profile": LTX_A2V_BENCH_NONE_PROFILE},
        job_id="test-structural-failure",
    )
    runner = LTXAudioToVideoTaskRunner(backend=backend, experimental=True)
    monkeypatch.setattr(runner, "_validate_request", lambda *_args: None)
    with pytest.raises(
        NonRetryableTaskError,
        match="A2V_EXPERIMENTAL_STREAMING_STRUCTURE_INVALID",
    ):
        runner.run(
            request,
            {"image": tmp_path / "image.png", "audio": tmp_path / "speech.wav"},
            tmp_path,
        )
    assert len(calls) == 1

    # Production behavior is unchanged, and other errors are not misclassified.
    request.task = LTX_A2V_TASK
    normal = LTXAudioToVideoTaskRunner(backend=backend)
    monkeypatch.setattr(normal, "_validate_request", lambda *_args: None)
    with pytest.raises(TypeError, match="FeedForward"):
        normal.run(request, {"image": tmp_path, "audio": tmp_path}, tmp_path)

    def other_error(**_kwargs: object) -> None:
        raise TypeError("unrelated Python error")

    experimental = LTXAudioToVideoTaskRunner(
        backend=SimpleNamespace(generate=other_error), experimental=True
    )
    request.task = LTX_A2V_EXPERIMENTAL_TASK
    monkeypatch.setattr(experimental, "_validate_request", lambda *_args: None)
    with pytest.raises(TypeError, match="unrelated"):
        experimental.run(request, {"image": tmp_path, "audio": tmp_path}, tmp_path)


def test_promoted_reference_profile_is_production_only() -> None:
    from ai_video_factory.workers.ltx25.a2v import (
        LTX_A2V_EXPERIMENTS,
        LTX_A2V_RESIDENCY_PROFILES,
        LTXAudioToVideoParameters,
    )

    assert LTXAudioToVideoParameters().generation_profile == LTX_A2V_PRODUCTION_FAST_PROFILE
    assert LTX_A2V_RESIDENCY_PROFILES[LTX_A2V_PRODUCTION_FAST_PROFILE] == ("none", "eager")
    assert LTX_A2V_PRODUCTION_FAST_PROFILE not in LTX_A2V_EXPERIMENTS
    production = build_ltx25_runners(_local_settings("a2v"))
    assert production.task_names == (LTX_A2V_TASK,)
    experimental = LTXAudioToVideoTaskRunner(
        backend=SimpleNamespace(), experimental=True
    )
    request = SimpleNamespace(
        task=LTX_A2V_EXPERIMENTAL_TASK,
        parameters={"generation_profile": LTX_A2V_PRODUCTION_FAST_PROFILE},
    )
    with pytest.raises(NonRetryableTaskError, match="experimental profile"):
        experimental._validate_request(request, {})
