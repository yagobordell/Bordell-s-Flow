"""Strict E1/E2 contracts; no generated content may mutate the B production plan."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpokenAnchor(StrictModel):
    beat_id: str = Field(min_length=2, max_length=32)
    phrase: str = Field(min_length=1, max_length=180)
    occurrence: int = Field(default=1, ge=1, le=20)


class EditorialElement(StrictModel):
    text: str = Field(min_length=1, max_length=100)
    anchor: SpokenAnchor


class LayoutIntent(StrictModel):
    placement: Literal[
        "center", "upper_center", "lower_center", "left", "right"
    ] = "center"
    scale: Literal["small", "medium", "large", "very_large"] = "large"
    color: str = "#FFFFFF"


class EditorialIntervention(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,64}$")
    kind: Literal["progressive_text", "number_emphasis", "accumulating_list", "text_card"]
    elements: list[EditorialElement] = Field(min_length=1, max_length=8)
    end_beat_id: str
    layout_intent: LayoutIntent
    animation: Literal["fade", "pop", "slide"] = "fade"

    @model_validator(mode="after")
    def size_matches_kind(self) -> EditorialIntervention:
        if self.kind in {"number_emphasis", "text_card"} and len(self.elements) != 1:
            raise ValueError("Single-element effect received multiple elements")
        return self


class E1Output(StrictModel):
    schema_version: Literal["e1-v1"]
    interventions: list[EditorialIntervention] = Field(max_length=200)

    @model_validator(mode="after")
    def unique_ids(self) -> E1Output:
        ids = [item.id for item in self.interventions]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicated editorial intervention ID")
        return self


class LayoutDecision(StrictModel):
    id: str
    status: Literal["approved", "adapted", "suppressed"]
    reason: str = Field(min_length=1, max_length=300)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    font_size_px: int = Field(ge=20, le=200)
    color: str
    stroke_color: str
    stroke_width_px: int = Field(ge=0, le=8)
    align: Literal["left", "center", "right"]

    @model_validator(mode="after")
    def on_screen(self) -> LayoutDecision:
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("Layout rectangle escapes visible frame")
        for value in (self.color, self.stroke_color):
            if len(value) != 7 or value[0] != "#" or any(
                char not in "0123456789ABCDEFabcdef" for char in value[1:]
            ):
                raise ValueError("Colors must be six-digit hexadecimal RGB")
        return self


class E2Output(StrictModel):
    schema_version: Literal["e2-v1"]
    decisions: list[LayoutDecision]
