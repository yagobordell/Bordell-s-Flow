"""Experimental A2V jobs are never claimed by the production worker or vice versa."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.inference.errors import NonRetryableTaskError
from ai_video_factory.workers.ltx25.a2v import (
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
