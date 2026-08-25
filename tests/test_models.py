from ai_video_factory.domain import (
    Beat,
    NarrativeBlock,
    ProjectConfig,
    Scene,
    SourceScript,
    StoryboardScene,
    VideoPlan,
)


def test_project_defaults() -> None:
    project = ProjectConfig(topic="La historia de los samuráis")

    assert project.language == "es"
    assert project.duration_seconds == 45
    assert project.aspect_ratio == "9:16"


def test_source_script_is_minimal_production_input() -> None:
    source = SourceScript(text="Un guion ya revisado y listo para producción.")

    assert source.model_dump() == {"text": "Un guion ya revisado y listo para producción."}


def test_narrative_planning_contracts_stay_minimal() -> None:
    block = NarrativeBlock(id=1, text="Los clanes guerreros empiezan a ganar poder.")
    beat = Beat(id=1, block_id=block.id, action="Los clanes acumulan poder político.")
    scene = Scene(id=1, beat_ids=[beat.id])

    assert block.model_dump() == {
        "id": 1,
        "text": "Los clanes guerreros empiezan a ganar poder.",
    }
    assert beat.model_dump() == {
        "id": 1,
        "block_id": 1,
        "action": "Los clanes acumulan poder político.",
    }
    assert scene.model_dump() == {"id": 1, "beat_ids": [1]}


def test_video_plan_keeps_legacy_storyboard_scenes() -> None:
    project = ProjectConfig(topic="La historia de los samuráis")
    scene = StoryboardScene(
        id=1,
        narration="Durante siglos, los samuráis marcaron la historia de Japón.",
        visual_prompt="Cinematic samurai overlooking feudal Japan at sunrise",
    )

    plan = VideoPlan(
        project=project,
        title="La historia de los samuráis",
        visual_style="cinematic historical documentary",
        scenes=[scene],
    )

    assert plan.scenes[0].id == 1
