from ai_video_factory.domain import ProjectConfig, Script


class ScriptWriterAgent:
    """Creates the narrative script from a project topic.

    OpenAI integration will be implemented in Phase 1.
    """

    async def run(self, project: ProjectConfig) -> Script:
        raise NotImplementedError("OpenAI integration arrives in Phase 1")
