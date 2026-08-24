from ai_video_factory.domain import ProjectConfig, Scene, VideoPlan


def test_project_defaults() -> None:
    project = ProjectConfig(topic="La historia de los samuráis")

    assert project.language == "es"
    assert project.duration_seconds == 45
    assert project.aspect_ratio == "9:16"


def test_video_plan_accepts_scenes() -> None:
    project = ProjectConfig(topic="La historia de los samuráis")
    scene = Scene(
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
