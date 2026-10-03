"""Validated, ebook-specific words for the casero CTA animation only.

All persistent QR instructions and generic English labels live in the Python
renderer; this model must never control them or claim unsupported offers.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class CTAMotionCopy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str
    benefits: list[str]

    @field_validator("title")
    @classmethod
    def title_length(cls, value: str) -> str:
        if not value or value != value.strip() or "\n" in value or len(value) > 72:
            raise ValueError("Motion title must be one trimmed line of at most 72 characters")
        return value

    @field_validator("benefits")
    @classmethod
    def benefit_lengths(cls, value: list[str]) -> list[str]:
        if len(value) > 3:
            raise ValueError("Motion allows at most three cover-supported benefits")
        if any(
            not item or item != item.strip() or "\n" in item or len(item) > 52
            for item in value
        ):
            raise ValueError("Each benefit must be one trimmed line of at most 52 characters")
        if len({item.casefold() for item in value}) != len(value):
            raise ValueError("Motion benefits must be distinct")
        return value
