"""Independently validate a source-preserving per-script casero CTA position."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator


class CTAPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    previous_sentence: str
    next_sentence: str

    @field_validator("previous_sentence", "next_sentence")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("CTA anchors cannot be empty")
        return value


class CTAEbook(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cta: str

    @field_validator("cta")
    @classmethod
    def spoken(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("CTA script cannot be empty")
        return value


def validate_placement(script: str, placement: CTAPlacement) -> int:
    """Return the unique verbatim insertion index; no silent fuzzy placement."""
    before, after = placement.previous_sentence, placement.next_sentence
    prev_positions = [m.start() for m in re.finditer(re.escape(before), script)]
    next_positions = [m.start() for m in re.finditer(re.escape(after), script)]
    if len(prev_positions) != 1 or len(next_positions) != 1:
        raise ValueError("CTA anchors must each occur exactly once in original script")
    end = prev_positions[0] + len(before)
    start = next_positions[0]
    if start <= end:
        raise ValueError("CTA anchors overlap or are out of order")
    between = script[end:start]
    # The attachment allows metadata-only headings between spoken sentences.
    if any(line.strip() and not line.lstrip().startswith("#") for line in between.splitlines()):
        raise ValueError("CTA anchors do not denote adjacent spoken sentences")
    if not (before.rstrip().endswith((".", "?", "!", "…", "。", "؟"))):
        raise ValueError("CTA previous anchor must be a complete spoken sentence")
    return end
