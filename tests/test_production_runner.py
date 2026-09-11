from pathlib import Path

import pytest

from ai_video_factory.workflows.production_runner import (
    ProductionGateRequired,
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
    assert manifest.stages["stage"].origin == "adopted"


def test_input_change_reruns_recorded_automatic_stage(tmp_path: Path) -> None:
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


def test_missing_manual_keyframe_artifact_stops_at_gate(tmp_path: Path) -> None:
    source = tmp_path / "storyboard.json"
    source.write_text("[]", encoding="utf-8")
    stage = ProductionStage(
        name="phase6-keyframes",
        description="manual keyframe gate",
        kind="manual_gate",
        inputs=(source,),
        outputs=(tmp_path / "storyboard_keyframes.json",),
        gate_message="choose the production keyframe model",
    )
    executor = RecordingExecutor()
    runner = ProductionRunner(
        [stage],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=executor,
    )

    with pytest.raises(ProductionGateRequired, match="choose the production keyframe model"):
        runner.run()

    assert executor.calls == []
    assert not (tmp_path / "manifest.json").exists()


def test_existing_manual_keyframes_are_adopted(tmp_path: Path) -> None:
    source = tmp_path / "storyboard.json"
    metadata = tmp_path / "storyboard_keyframes.json"
    artifacts = tmp_path / "storyboard_keyframes"
    source.write_text("[]", encoding="utf-8")
    metadata.write_text("[]", encoding="utf-8")
    artifacts.mkdir()
    (artifacts / "shot_001.png").write_bytes(b"png")
    runner = ProductionRunner(
        [
            ProductionStage(
                name="phase6-keyframes",
                description="manual keyframe gate",
                kind="manual_gate",
                inputs=(source,),
                outputs=(metadata, artifacts),
            )
        ],
        manifest_path=tmp_path / "manifest.json",
        repo_root=tmp_path,
        executor=RecordingExecutor(),
    )

    summary = runner.run()

    assert summary.adopted == ("phase6-keyframes",)


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


def test_production_dag_prepares_video_prompts_before_keyframe_gate(tmp_path: Path) -> None:
    stages = build_production_stages(
        script_file=tmp_path / "script.txt",
        output_dir=tmp_path / "output",
    )
    names = [stage.name for stage in stages]
    keyframe = next(stage for stage in stages if stage.name == "phase6-keyframes")

    assert names.index("phase8-video-prompts") < names.index("phase6-keyframes")
    assert names.index("phase6-keyframes") < names.index("phase8-videos")
    assert keyframe.kind == "manual_gate"
    assert keyframe.script is None
    assert "reference_assets.json" in {path.name for path in keyframe.inputs}


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
