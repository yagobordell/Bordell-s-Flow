"""Detailed, bounded feedback for recoverable structured bot validations."""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

MAX_VALIDATION_RETRIES = 2


def is_schema_failure(error: Exception) -> bool:
    return isinstance(error, ValidationError) or (
        isinstance(error, RuntimeError)
        and str(error).startswith(
            ("OpenAI returned no parsed structured output",
             "OpenAI returned an unexpected structured output type")
        )
    )


def validation_feedback(
    stage: str,
    error: Exception,
    *,
    block_id: int | None = None,
    previous: dict[str, Any] | None = None,
    continued: bool = False,
    source_script: str | None = None,
    instructions: str,
) -> str:
    """Give the model actionable field-level errors, not a generic retry request."""
    heading = f"VALIDATION FAILED: {stage}"
    if block_id is not None:
        heading += f" / block {block_id}"
    diagnostics = [f"Failure: {error}"]
    if isinstance(error, ValidationError):
        diagnostics = [
            "Invalid field " + ".".join(map(str, item["loc"])) + ": " + item["msg"]
            for item in error.errors(include_url=False)
        ]
    if stage == "B1.1" and source_script and previous:
        # Anchors can be repeated independently even when the complete tiling
        # is unique. Give occurrence counts as diagnostics, not as a constraint.
        normalized = " ".join(source_script.split())
        for block in previous.get("blocks") or []:
            span = block.get("span") or {}
            for field in ("first_words", "last_words"):
                value = span.get(field)
                if isinstance(value, str) and value:
                    count = normalized.count(" ".join(value.split()))
                    if count != 1 or any(
                        term in str(error).lower() for term in ('tile', 'ambiguous')
                    ):
                        diagnostics.append(
                            f"Block {block.get('block_id')} span.{field}: "
                            f"{value!r} has {count} occurrence(s), first at "
                            f"offset {normalized.find(chr(32).join(value.split()))}, "
                            "in the normalized source. "
                            "Check the intended boundary and the ordered full-script tiling."
                        )
    lines = [
        heading,
        "Validator diagnostics:",
        *(f"- {item}" for item in diagnostics),
        "Required correction:",
        instructions,
        "Return the COMPLETE corrected object in the original structured output schema.",
        "Re-check ALL applicable invariants before answering.",
    ]
    if previous is not None and not continued:
        lines.extend((
            "Previous rejected response (repair this response where possible):",
            json.dumps(previous, ensure_ascii=False, separators=(",", ":")),
        ))
    if previous is None:
        lines.append(
            "The previous response could not be parsed; "
            "rebuild the full object from the original input."
        )
    return "\n".join(lines)
