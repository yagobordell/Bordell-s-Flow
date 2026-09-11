import json
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
    assert "1024x1536" in keyframe.arguments
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
