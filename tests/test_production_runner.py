import json
import threading
import time
from pathlib import Path

import pytest

from ai_video_factory.workflows.production_runner import (
    ProductionRunManifest,
    ProductionRunner,
    ProductionStage,
    ProductionStageBlocked,
    build_production_stages,
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, stage: ProductionStage) -> None:
        self.calls.append(stage.name)
        for output in stage.outputs:
            if output.suffix:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(f"generated:{stage.name}\n", encoding="utf-8")
            else:
                output.mkdir(parents=True, exist_ok=True)
                (output / "artifact.txt").write_text(stage.name, encoding="utf-8")


def _script(tmp_path: Path, name: str = "stage.py") -> Path:
    path = tmp_path / name
    path.write_text("print('stage')\n", encoding="utf-8")
    return path


def test_existing_stage_is_adopted_then_skipped(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    output = tmp_path / "output.json"
    source.write_text("input", encoding="utf-8")
    output.write_text("existing", encoding="utf-8")
    executor = RecordingExecutor()
    runner = ProductionRunner(
        [
            ProductionStage(
                name="stage",
                description="test",
                script=_script(tmp_path),
                inputs=(source,),
                outputs=(output,),
            )
        ],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=executor,
    )

    first = runner.run()
    second = runner.run()

    assert first.adopted == ("stage",)
    assert second.skipped == ("stage",)
    assert executor.calls == []
    manifest = ProductionRunManifest.model_validate_json(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.schema_version == "2"
    assert manifest.stages["stage"].origin == "adopted"


def test_input_change_reruns_recorded_stage(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    output = tmp_path / "output.json"
    source.write_text("v1", encoding="utf-8")
    output.write_text("existing", encoding="utf-8")
    executor = RecordingExecutor()
    runner = ProductionRunner(
        [
            ProductionStage(
                name="stage",
                description="test",
                script=_script(tmp_path),
                inputs=(source,),
                outputs=(output,),
            )
        ],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=executor,
    )
    runner.run()

    source.write_text("v2", encoding="utf-8")
    summary = runner.run()

    assert summary.executed == ("stage",)
    assert executor.calls == ["stage"]
    assert output.read_text(encoding="utf-8") == "generated:stage\n"


def test_script_change_invalidates_recorded_stage(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    output = tmp_path / "output.json"
    source.write_text("input", encoding="utf-8")
    output.write_text("existing", encoding="utf-8")
    script = _script(tmp_path)
    executor = RecordingExecutor()
    runner = ProductionRunner(
        [
            ProductionStage(
                name="stage",
                description="test",
                script=script,
                inputs=(source,),
                outputs=(output,),
            )
        ],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=executor,
    )
    runner.run()

    script.write_text("print('changed')\n", encoding="utf-8")
    summary = runner.run()

    assert summary.executed == ("stage",)
    assert executor.calls == ["stage"]


def test_missing_stage_input_is_blocked(tmp_path: Path) -> None:
    stage = ProductionStage(
        name="stage",
        description="test",
        script=_script(tmp_path),
        inputs=(tmp_path / "missing.json",),
        outputs=(tmp_path / "output.json",),
    )
    runner = ProductionRunner(
        [stage],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=RecordingExecutor(),
    )

    with pytest.raises(ProductionStageBlocked, match="missing inputs"):
        runner.run()


def test_production_dag_automates_ideogram_keyframes(tmp_path: Path) -> None:
    stages = build_production_stages(
        script_file=tmp_path / "script.txt",
        output_dir=tmp_path / "output",
    )
    names = [stage.name for stage in stages]
    keyframe = next(stage for stage in stages if stage.name == "phase6-keyframes")

    assert names.index("phase8-video-prompts") < names.index("phase6-keyframes")
    assert names.index("phase6-keyframes") < names.index("phase8-videos")
    assert keyframe.script == Path("scripts/run_phase6_keyframes.py")
    assert "--quality" in keyframe.arguments
    assert "high" in keyframe.arguments
    assert "--size" in keyframe.arguments
    assert "1536x864" in keyframe.arguments
    assert {path.name for path in keyframe.inputs} == {
        "storyboard_frames.json",
        "shots.json",
    }
    assert "reference_assets.json" not in {path.name for path in keyframe.inputs}


def test_existing_keyframes_are_adopted_then_stale_inputs_regenerate(tmp_path: Path) -> None:
    source = tmp_path / "storyboard.json"
    metadata = tmp_path / "storyboard_keyframes.json"
    artifacts = tmp_path / "storyboard_keyframes"
    script = _script(tmp_path, "keyframes.py")
    source.write_text("[]", encoding="utf-8")
    metadata.write_text("[]", encoding="utf-8")
    artifacts.mkdir()
    (artifacts / "shot_001.png").write_bytes(b"png")
    executor = RecordingExecutor()
    runner = ProductionRunner(
        [
            ProductionStage(
                name="phase6-keyframes",
                description="Ideogram keyframes",
                script=script,
                inputs=(source,),
                outputs=(metadata, artifacts),
            )
        ],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=executor,
    )

    first = runner.run()
    source.write_text("[1]", encoding="utf-8")
    second = runner.run()

    assert first.adopted == ("phase6-keyframes",)
    assert second.executed == ("phase6-keyframes",)
    assert executor.calls == ["phase6-keyframes"]


def test_v1_manifest_drops_old_manual_gate_record(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    output = tmp_path / "output.json"
    source.write_text("input", encoding="utf-8")
    output.write_text("existing", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "stages": {
                    "phase6-keyframes": {
                        "stage_name": "phase6-keyframes",
                        "kind": "manual_gate",
                        "spec_sha256": "old",
                        "input_sha256": "old",
                        "output_sha256": "old",
                        "origin": "adopted",
                        "command": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    runner = ProductionRunner(
        [
            ProductionStage(
                name="stage",
                description="test",
                script=_script(tmp_path),
                inputs=(source,),
                outputs=(output,),
            )
        ],
        manifest_path=manifest_path,
        repo_root=tmp_path,
        executor=RecordingExecutor(),
    )

    summary = runner.run()

    assert summary.adopted == ("stage",)
    upgraded = ProductionRunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    assert upgraded.schema_version == "2"
    assert "phase6-keyframes" not in upgraded.stages


def test_plan_does_not_mutate_manifest(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    output = tmp_path / "output.json"
    source.write_text("input", encoding="utf-8")
    output.write_text("existing", encoding="utf-8")
    runner = ProductionRunner(
        [
            ProductionStage(
                name="stage",
                description="test",
                script=_script(tmp_path),
                inputs=(source,),
                outputs=(output,),
            )
        ],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=RecordingExecutor(),
    )

    inspection = runner.plan()

    assert inspection[0].status == "adoptable"
    assert not (tmp_path / "manifest.json").exists()



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
    assert summary.total_elapsed_seconds < executor.delay_seconds * 1.8


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
    assert by_name["phase4-reference-assets"].resource_key == "ideogram4"
    assert by_name["phase6-keyframes"].resource_key == "ideogram4"
    assert by_name["phase8-upscale"].dependencies == ("phase8-videos",)
    assert by_name["phase8-upscale"].resource_key == "realesrgan"
    assert by_name["phase8-upscale"].outputs[0].name == "upscaled_clips.json"
