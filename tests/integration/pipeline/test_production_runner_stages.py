import json
from pathlib import Path

import pytest
from _production_runner_support import RecordingExecutor, _script

from ai_video_factory.workflows.production_runner import (
    ProductionRunManifest,
    ProductionRunner,
    ProductionStage,
    build_production_stages,
)


def test_production_dag_automates_qwen_keyframes(tmp_path: Path) -> None:
    stages = build_production_stages(
        script_file=tmp_path / "script.txt",
        output_dir=tmp_path / "output",
    )
    names = [stage.name for stage in stages]
    keyframe = next(stage for stage in stages if stage.name == "phase6-keyframes")
    references = next(stage for stage in stages if stage.name == "phase4-reference-assets")
    storyboard = next(stage for stage in stages if stage.name == "phase6-storyboard")
    video_prompts = next(stage for stage in stages if stage.name == "phase8-video-prompts")
    videos = next(stage for stage in stages if stage.name == "phase8-videos")

    assert names.index("phase8-video-prompts") < names.index("phase6-keyframes")
    assert names.index("phase6-keyframes") < names.index("phase8-videos")
    assert keyframe.script == Path("scripts/pipeline/run_phase6_keyframes.py")
    assert "--quality" in keyframe.arguments
    assert "high" in keyframe.arguments
    assert "--size" in keyframe.arguments
    assert "1280x736" in keyframe.arguments
    assert {path.name for path in keyframe.inputs} == {
        "storyboard_frames.json",
        "shots.json",
    }
    assert "reference_assets.json" not in {path.name for path in keyframe.inputs}
    assert "1280x736" in references.arguments
    assert "16:9" in storyboard.arguments
    assert "16:9" in video_prompts.arguments
    assert "1280" in videos.arguments
    assert "720" in videos.arguments
    assert "24" in videos.arguments

    alignment = next(stage for stage in stages if stage.name == "phase5-alignment")
    assert "--language" in alignment.arguments
    language_index = alignment.arguments.index("--language")
    assert alignment.arguments[language_index + 1] == "en"


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
                description="Qwen keyframes",
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


def test_production_alignment_language_is_explicit_and_changes_stage_spec(
    tmp_path: Path,
) -> None:
    script_file = tmp_path / "script.txt"
    script_file.write_text("hola mundo", encoding="utf-8")
    output = tmp_path / "output"

    english = build_production_stages(
        script_file=script_file,
        output_dir=output,
        narration_language="en",
    )
    spanish = build_production_stages(
        script_file=script_file,
        output_dir=output,
        narration_language="es",
    )

    english_alignment = next(stage for stage in english if stage.name == "phase5-alignment")
    spanish_alignment = next(stage for stage in spanish if stage.name == "phase5-alignment")

    assert english_alignment.arguments != spanish_alignment.arguments
    assert "en" in english_alignment.arguments
    assert "es" in spanish_alignment.arguments


def test_production_rejects_empty_narration_language(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="narration language must be non-empty"):
        build_production_stages(
            script_file=tmp_path / "script.txt",
            output_dir=tmp_path / "output",
            narration_language="   ",
        )
