"""Fish Audio script director: strict insertion-only annotations of raw narration."""

from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import Any

from pydantic import BaseModel, ConfigDict

from ai_video_factory.providers.base import StructuredTextProvider

_FISH_TAG = re.compile(r"\[[A-Za-z][A-Za-z -]{0,79}\]", flags=re.ASCII)
_STRUCTURED_SUFFIX = (
    "\n\nAPI transport: this request uses Responses API Structured Outputs. "
    "Return the exact JSON object through the supplied schema, with no Markdown fence. "
    "Do not rewrite, normalize or modify source characters; insert Fish tags only."
)


class FishAudioScriptError(ValueError):
    """The voice-directed script is not an insertion-only version of the source."""


class FishAudioScript(BaseModel):
    """The uploaded prompt requires exactly this single top-level JSON field."""

    model_config = ConfigDict(extra="forbid")

    plain_script_for_recording: str


def validate_fish_script(original: str, directed: str) -> tuple[str, ...]:
    """Prove that removing *only newly inserted* square-bracket tags restores source."""
    if not isinstance(original, str) or not original.strip():
        raise FishAudioScriptError("Fish Audio requires a nonempty source script")
    if not isinstance(directed, str):
        raise FishAudioScriptError("Fish Audio output must be a string")

    source_index = 0
    result_index = 0
    inserted: list[str] = []
    while result_index < len(directed):
        match = _FISH_TAG.match(directed, result_index)
        if match is not None:
            tag = match.group()
            # Existing bracketed source content belongs to the original script.
            # When a new tag precedes that content, require the source to follow
            # immediately after it; never silently reclassify original text.
            is_original = original.startswith(tag, source_index)
            next_source = original[source_index : source_index + 24]
            follows_source = bool(next_source) and directed[
                match.end() :
            ].startswith(next_source)
            if not is_original and (
                follows_source
                or source_index >= len(original)
                or directed[result_index] != original[source_index]
            ):
                if source_index >= len(original):
                    raise FishAudioScriptError("Fish direction cannot trail the final speech")
                if (
                    source_index > 0
                    and original[source_index - 1].isalnum()
                    and original[source_index].isalnum()
                ):
                    raise FishAudioScriptError("Fish direction cannot split a word")
                if not any(character.isalnum() for character in original[source_index:]):
                    raise FishAudioScriptError("Fish direction must precede spoken text")
                inserted.append(tag)
                result_index = match.end()
                continue

        if (
            source_index >= len(original)
            or directed[result_index] != original[source_index]
        ):
            raise FishAudioScriptError(
                "Fish Audio output changed the source or inserted non-Fish text"
            )
        source_index += 1
        result_index += 1

    if source_index != len(original):
        raise FishAudioScriptError("Fish Audio output omitted source characters")
    return tuple(inserted)


def fish_prompt_bytes() -> bytes:
    """Read the user's prompt verbatim; there are no pinned prompt hashes."""
    return files("ai_video_factory.bots.prompts").joinpath("fish_audio.md").read_bytes()


async def run_fish_director(
    script: str,
    *,
    provider: StructuredTextProvider,
    model: str,
) -> tuple[FishAudioScript, Any | None]:
    """Feed precisely the same raw JSON payload that B1.1 receives."""
    input_text = json.dumps(
        {"plain_script_for_recording": script},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": fish_prompt_bytes().decode("utf-8") + _STRUCTURED_SUFFIX,
        "input_text": input_text,
        "output_type": FishAudioScript,
    }
    metered = getattr(provider, "generate_structured_with_response", None)
    if callable(metered):
        output, response = await metered(**kwargs)
    else:
        output, response = await provider.generate_structured(**kwargs), None
    if not isinstance(output, FishAudioScript):
        output = FishAudioScript.model_validate(output)
    validate_fish_script(script, output.plain_script_for_recording)
    return output, response
