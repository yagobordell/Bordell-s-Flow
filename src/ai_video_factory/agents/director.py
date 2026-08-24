from ai_video_factory.domain import ProjectConfig, Script, VideoPlan


class DirectorAgent:
    """Transforms a script into a structured audiovisual plan."""

    async def run(self, project: ProjectConfig, script: Script) -> VideoPlan:
        raise NotImplementedError("OpenAI integration arrives in Phase 1")
