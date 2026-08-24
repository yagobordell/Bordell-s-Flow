from ai_video_factory.agents.director import DirectorAgent
from ai_video_factory.agents.scriptwriter import ScriptWriterAgent
from ai_video_factory.domain import ProjectConfig, VideoPlan


async def create_video_plan(
    topic: str,
    *,
    writer: ScriptWriterAgent,
    director: DirectorAgent,
) -> VideoPlan:
    """First workflow: topic -> script -> video plan."""

    project = ProjectConfig(topic=topic)
    script = await writer.run(project)
    return await director.run(project, script)
