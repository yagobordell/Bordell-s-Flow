from ai_video_factory.agents.director import DirectorAgent
from ai_video_factory.agents.scriptwriter import ScriptWriterAgent
from ai_video_factory.domain import ProjectConfig, Script, VideoPlan


async def create_script_and_plan(
    topic: str,
    *,
    writer: ScriptWriterAgent,
    director: DirectorAgent,
) -> tuple[ProjectConfig, Script, VideoPlan]:
    """Phase 1 workflow: topic -> project -> script -> video plan."""

    project = ProjectConfig(topic=topic)
    script = await writer.run(project)
    plan = await director.run(project, script)
    return project, script, plan


async def create_video_plan(
    topic: str,
    *,
    writer: ScriptWriterAgent,
    director: DirectorAgent,
) -> VideoPlan:
    """Convenience wrapper that returns only the final VideoPlan."""

    _, _, plan = await create_script_and_plan(
        topic,
        writer=writer,
        director=director,
    )
    return plan
