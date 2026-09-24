import threading
import time
from pathlib import Path

import pytest
from _production_runner_support import _script

from ai_video_factory.workflows.production_runner import (
    ProductionRunner,
    ProductionStage,
    build_production_stages,
)


class ParallelRecordingExecutor:
    def __init__(self, *, delay_seconds: float = 0.05) -> None:
        self.delay_seconds = delay_seconds
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.active_by_resource: dict[str, int] = {}
        self.max_by_resource: dict[str, int] = {}
        self.lock = threading.Lock()

    def __call__(self, stage: ProductionStage) -> None:
        key = stage.resource_key or stage.name
        with self.lock:
            self.calls.append(stage.name)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.active_by_resource[key] = self.active_by_resource.get(key, 0) + 1
            self.max_by_resource[key] = max(
                self.max_by_resource.get(key, 0),
                self.active_by_resource[key],
            )
        try:
            time.sleep(self.delay_seconds)
            for output in stage.outputs:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(stage.name, encoding="utf-8")
        finally:
            with self.lock:
                self.active -= 1
                self.active_by_resource[key] -= 1


def test_dag_runs_independent_stages_in_parallel(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    executor = ParallelRecordingExecutor()
    runner = ProductionRunner(
        [
            ProductionStage(
                name="a",
                description="a",
                script=_script(tmp_path, "a.py"),
                inputs=(source,),
                outputs=(tmp_path / "a.json",),
            ),
            ProductionStage(
                name="b",
                description="b",
                script=_script(tmp_path, "b.py"),
                inputs=(source,),
                outputs=(tmp_path / "b.json",),
            ),
        ],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=executor,
        max_workers=2,
        max_gpu_stages=2,
    )

    summary = runner.run()

    assert set(summary.executed) == {"a", "b"}
    assert executor.max_active == 2


def test_gpu_concurrency_is_bounded_and_same_resource_never_overlaps(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    executor = ParallelRecordingExecutor()
    stages = [
        ProductionStage(
            name=f"gpu-{index}",
            description="gpu",
            script=_script(tmp_path, f"gpu_{index}.py"),
            inputs=(source,),
            outputs=(tmp_path / f"gpu_{index}.json",),
            resource="gpu",
            resource_key="ideogram4",
        )
        for index in range(3)
    ]
    runner = ProductionRunner(
        stages,
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=executor,
        max_workers=3,
        max_gpu_stages=2,
    )

    runner.run()

    assert executor.max_by_resource["ideogram4"] == 1
    assert executor.max_active == 1


def test_failed_upstream_stage_never_runs_its_dependents(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    calls: list[str] = []

    def executor(stage: ProductionStage) -> None:
        calls.append(stage.name)
        if stage.name == "root":
            raise RuntimeError("boom")
        stage.outputs[0].write_text("unexpected", encoding="utf-8")

    runner = ProductionRunner(
        [
            ProductionStage(
                name="root",
                description="root",
                script=_script(tmp_path, "root.py"),
                inputs=(source,),
                outputs=(tmp_path / "root.json",),
            ),
            ProductionStage(
                name="child",
                description="child",
                script=_script(tmp_path, "child.py"),
                inputs=(tmp_path / "root.json",),
                outputs=(tmp_path / "child.json",),
                dependencies=("root",),
            ),
        ],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=executor,
        max_workers=2,
    )

    with pytest.raises(RuntimeError, match="boom"):
        runner.run()

    assert calls == ["root"]
    assert not (tmp_path / "child.json").exists()


def test_real_production_dag_exposes_safe_parallel_branches(tmp_path: Path) -> None:
    stages = build_production_stages(
        script_file=tmp_path / "script.txt",
        output_dir=tmp_path / "output",
    )
    by_name = {stage.name: stage for stage in stages}

    assert by_name["phase3-continuity"].dependencies == ("phase2-narrative",)
    assert by_name["phase5-narration"].dependencies == ("phase2-narrative",)
    assert by_name["phase3-shots"].dependencies == ("phase3-continuity",)
    assert by_name["phase4-reference-prompts"].dependencies == ("phase3-continuity",)
    assert by_name["phase8-video-prompts"].dependencies == ("phase6-storyboard",)
    assert by_name["phase6-keyframes"].dependencies == ("phase6-storyboard",)
    assert by_name["phase4-reference-assets"].resource_key == "qwen_image_21"
    assert by_name["phase6-keyframes"].resource_key == "qwen_image_21"
    assert by_name["phase8-upscale"].dependencies == ("phase8-videos",)
    assert by_name["phase8-upscale"].resource_key == "realesrgan"
    assert by_name["phase8-upscale"].outputs[0].name == "upscaled_clips.json"
    assert Path("src/ai_video_factory/providers/salad_breeze.py") in by_name[
        "phase5-narration"
    ].inputs
    assert Path("src/ai_video_factory/workers/breeze_tts2/model.py") in by_name[
        "phase5-narration"
    ].inputs


class FailFastExecutor:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.sibling_started = threading.Event()
        self.cancel_calls = 0

    def __call__(self, stage: ProductionStage) -> None:
        if stage.name == "failing":
            assert self.sibling_started.wait(timeout=2)
            raise RuntimeError("boom")
        self.sibling_started.set()
        assert self.release.wait(timeout=2)

    def cancel_running(self) -> None:
        self.cancel_calls += 1
        self.release.set()


def test_dag_failure_cancels_active_sibling_before_waiting_for_pool(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    executor = FailFastExecutor()
    runner = ProductionRunner(
        [
            ProductionStage(
                name="failing",
                description="failing",
                script=_script(tmp_path, "failing.py"),
                inputs=(source,),
                outputs=(tmp_path / "failing.json",),
            ),
            ProductionStage(
                name="sibling",
                description="sibling",
                script=_script(tmp_path, "sibling.py"),
                inputs=(source,),
                outputs=(tmp_path / "sibling.json",),
            ),
        ],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=executor,
        max_workers=2,
    )

    with pytest.raises(RuntimeError, match="boom"):
        runner.run()

    assert executor.cancel_calls == 1
    assert executor.release.is_set()


class HealthAwareExecutor:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.cancel_calls = 0

    def __call__(self, stage: ProductionStage) -> None:
        self.started.set()
        assert self.release.wait(timeout=2)
        if not stage.outputs[0].exists():
            stage.outputs[0].write_text("cancelled", encoding="utf-8")

    def cancel_running(self) -> None:
        self.cancel_calls += 1
        self.release.set()


def test_capacity_health_failure_cancels_running_stages(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    executor = HealthAwareExecutor()
    checks = 0

    def health_check() -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise RuntimeError("capacity controller unhealthy")

    runner = ProductionRunner(
        [
            ProductionStage(
                name="gpu",
                description="gpu",
                script=_script(tmp_path, "gpu.py"),
                inputs=(source,),
                outputs=(tmp_path / "gpu.json",),
                resource="gpu",
            )
        ],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=executor,
        max_workers=1,
        max_gpu_stages=1,
        health_check=health_check,
        health_check_interval_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="capacity controller unhealthy"):
        runner.run()

    assert executor.started.is_set()
    assert executor.cancel_calls == 1
    assert executor.release.is_set()
