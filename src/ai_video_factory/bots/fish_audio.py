"""Fish Audio direction for one immutable B1.1 materialized block."""

from __future__ import annotations

import json
import re
import unicodedata
from importlib.resources import files
from typing import Any

from pydantic import BaseModel, ConfigDict

from ai_video_factory.providers.base import StructuredTextProvider

from .contracts import B12Input

_FISH_TAG = re.compile(r"\[[A-Za-z][A-Za-z -]{0,79}\]", flags=re.ASCII)
_STRUCTURED_SUFFIX = (
    "\n\nAPI transport: this request uses Responses API Structured Outputs. "
    "Return the exact JSON object through the supplied schema, with no Markdown fence. "
    "Never add, remove, replace or reorder spoken words; insert Fish tags only."
)


class FishAudioScriptError(ValueError):
    """The directed script changed the source beyond allowed layout/punctuation."""


class FishAudioScript(BaseModel):
    """The Fish director has exactly one output field."""

    model_config = ConfigDict(extra="forbid")

    plain_script_for_recording: str


def _ignorable(character: str) -> bool:
    """Match B1.2's text validator: ignore whitespace and Unicode punctuation."""
    return character.isspace() or unicodedata.category(character).startswith("P")


def validate_fish_script(original: str, directed: str) -> tuple[str, ...]:
    """Identify new Fish tags; tolerate layout/punctuation but never changed words.

    Existing bracketed source text is not treated as a newly inserted tag.
    The director prompt still requires byte-for-byte insertion-only preservation;
    this guard has the same whitespace/punctuation tolerance as B1.2.
    """
    if not isinstance(original, str) or not original.strip():
        raise FishAudioScriptError("Fish Audio requires a nonempty source script")
    if not isinstance(directed, str):
        raise FishAudioScriptError("Fish Audio output must be a string")

    source_index = 0
    result_index = 0
    inserted: list[str] = []
    while result_index < len(directed):
        match = _FISH_TAG.match(directed, result_index)
        if match is not None and not original.startswith(match.group(), source_index):
            if not any(character.isalnum() for character in original[source_index:]):
                raise FishAudioScriptError("Fish direction cannot trail the final speech")
            if (
                source_index > 0
                and source_index < len(original)
                and original[source_index - 1].isalnum()
                and original[source_index].isalnum()
            ):
                raise FishAudioScriptError("Fish direction cannot split a word")
            inserted.append(match.group())
            result_index = match.end()
            continue

        if (
            source_index < len(original)
            and original[source_index] == directed[result_index]
        ):
            source_index += 1
            result_index += 1
        elif source_index < len(original) and _ignorable(original[source_index]):
            source_index += 1
        elif _ignorable(directed[result_index]):
            result_index += 1
        else:
            raise FishAudioScriptError(
                "Fish Audio output changed the source beyond whitespace or punctuation"
            )

    if any(not _ignorable(c) for c in original[source_index:]):
        raise FishAudioScriptError("Fish Audio output omitted source characters")
    return tuple(inserted)


def fish_prompt_bytes() -> bytes:
    """Read the user's current per-block prompt without pinned hashes."""
    return files("ai_video_factory.bots.prompts").joinpath("fish_audio.md").read_bytes()


async def run_fish_director(
    payload: B12Input,
    *,
    provider: StructuredTextProvider,
    model: str,
) -> tuple[FishAudioScript, Any | None]:
    """Send the exact same per-block JSON input shape used by B1.2."""
    block = payload.blocks[0]
    input_text = json.dumps(payload.model_dump(), ensure_ascii=False, separators=(",", ":"))
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
    validate_fish_script(block.text, output.plain_script_for_recording)
    return output, response
