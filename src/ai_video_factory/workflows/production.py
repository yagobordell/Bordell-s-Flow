from ai_video_factory.domain import Script, SourceScript


def prepare_source_script(script_text: str) -> SourceScript:
    """Create the canonical production input from a user-provided script."""

    return SourceScript(text=script_text)


def prepare_generated_script(script: Script) -> SourceScript:
    """Adapt the optional Phase 1 generated script to the production pipeline."""

    return SourceScript.from_generated(script)
