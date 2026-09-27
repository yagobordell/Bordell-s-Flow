"""The two LTX groups must not claim each other's Postgres jobs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_video_factory.inference.errors import UnsupportedTaskError
from ai_video_factory.inference.salad_capacity import _SERVICE_TASKS
from ai_video_factory.workers.ltx25.a2v import LTX_A2V_TASK
from ai_video_factory.workers.ltx25.model import LTX_VIDEO_TASK
from ai_video_factory.workers.ltx25.settings import LTX25WorkerSettings
from ai_video_factory.workers.ltx25.worker_factory import build_ltx25_runners


@pytest.mark.parametrize(
    ("mode", "owned", "foreign"),
    [
        ("i2v", LTX_VIDEO_TASK, LTX_A2V_TASK),
        ("a2v", LTX_A2V_TASK, LTX_VIDEO_TASK),
    ],
)
def test_ltx_group_claims_only_its_own_task(
    tmp_path: Path, mode: str, owned: str, foreign: str
) -> None:
    settings = LTX25WorkerSettings(
        _env_file=None,
        worker_mode="local",
        worker_poll_jobs=True,
        task_mode=mode,
        model_root=tmp_path,
    )
    runners = build_ltx25_runners(settings)
    assert runners.task_names == (owned,)
    with pytest.raises(UnsupportedTaskError):
        runners.get(foreign)


def test_predictive_capacity_never_combines_avatar_and_i2v_demand() -> None:
    assert _SERVICE_TASKS["ltx25_i2v"] == (LTX_VIDEO_TASK,)
    assert _SERVICE_TASKS["ltx25"] == (LTX_A2V_TASK,)
    manifest = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    i2v = manifest["services"]["ltx25_i2v"]
    avatar = manifest["services"]["ltx25"]
    assert i2v["group_name"] != avatar["group_name"]
    assert i2v["environment"]["LTX_WORKER_TASK_MODE"] == "i2v"
    assert avatar["environment"]["LTX_WORKER_TASK_MODE"] == "a2v"
