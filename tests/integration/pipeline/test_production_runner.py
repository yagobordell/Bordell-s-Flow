from pathlib import Path

import pytest
from _production_runner_support import RecordingExecutor, _script

from ai_video_factory.workflows.production_runner import (
    ProductionRunManifest,
    ProductionRunner,
    ProductionStage,
    ProductionStageBlocked,
)


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
