from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IdeogramStylePlan(BaseModel):
    """Model-owned style plan rendered into Ideogram's ordered JSON schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aesthetics: str = Field(min_length=1, max_length=2000)
    lighting: str = Field(min_length=1, max_length=2000)
    medium: str = Field(min_length=1, max_length=1000)
    render_mode: Literal["photo", "art"]
    render_description: str = Field(min_length=1, max_length=2000)
    color_palette: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("color_palette")
    @classmethod
    def validate_color_palette(cls, values: list[str]) -> list[str]:
        return [_normalize_hex_color(value) for value in values]


class IdeogramElementPlan(BaseModel):
    """One non-text visual element in an Ideogram composition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = Field(min_length=1, max_length=4000)
    bbox: list[int] | None = Field(default=None, min_length=4, max_length=4)
    color_palette: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(item < 0 or item > 1000 for item in value):
            raise ValueError("Ideogram bbox coordinates must be between 0 and 1000")
        ymin, xmin, ymax, xmax = value
        if ymin > ymax or xmin > xmax:
            raise ValueError("Ideogram bbox coordinates must preserve min/max ordering")
        return value

    @field_validator("color_palette")
    @classmethod
    def validate_color_palette(cls, values: list[str]) -> list[str]:
        return [_normalize_hex_color(value) for value in values]


class IdeogramCaptionPlan(BaseModel):
    """Provider-neutral plan serialized into the exact Ideogram 4 caption structure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    high_level_description: str = Field(min_length=1, max_length=6000)
    style: IdeogramStylePlan
    background: str = Field(min_length=1, max_length=5000)
    elements: list[IdeogramElementPlan] = Field(default_factory=list, max_length=24)


def render_ideogram_caption(plan: IdeogramCaptionPlan) -> str:
    """Serialize a plan using the key ordering required by Ideogram's verifier."""

    style: dict[str, Any] = {
        "aesthetics": plan.style.aesthetics,
        "lighting": plan.style.lighting,
    }
    if plan.style.render_mode == "photo":
        style["photo"] = plan.style.render_description
        style["medium"] = plan.style.medium
    else:
        style["medium"] = plan.style.medium
        style["art_style"] = plan.style.render_description
    if plan.style.color_palette:
        style["color_palette"] = plan.style.color_palette

    elements: list[dict[str, Any]] = []
    for element in plan.elements:
        item: dict[str, Any] = {"type": "obj"}
        if element.bbox is not None:
            item["bbox"] = element.bbox
        item["desc"] = element.description
        if element.color_palette:
            item["color_palette"] = element.color_palette
        elements.append(item)

    caption = {
        "high_level_description": plan.high_level_description,
        "style_description": style,
        "compositional_deconstruction": {
            "background": plan.background,
            "elements": elements,
        },
    }
    return json.dumps(caption, ensure_ascii=False, separators=(",", ":"))


def validate_ideogram_caption(raw: str) -> str:
    """Reject prompts that are not compatible with the local Ideogram caption contract."""

    try:
        caption = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Ideogram 4 prompt must be a structured JSON caption") from exc
    if not isinstance(caption, dict):
        raise ValueError("Ideogram 4 caption root must be a JSON object")

    allowed_root = {
        "high_level_description",
        "style_description",
        "compositional_deconstruction",
    }
    if set(caption) - allowed_root:
        raise ValueError("Ideogram 4 caption contains unsupported top-level keys")

    composition = caption.get("compositional_deconstruction")
    if not isinstance(composition, dict):
        raise ValueError("Ideogram 4 caption requires compositional_deconstruction")
    if not isinstance(composition.get("background"), str):
        raise ValueError("Ideogram 4 caption requires a textual background description")
    elements = composition.get("elements")
    if not isinstance(elements, list):
        raise ValueError("Ideogram 4 caption requires an elements array")
    if any(not isinstance(item, dict) or item.get("type") != "obj" for item in elements):
        raise ValueError("AI Video Factory Ideogram captions support only object elements")

    style = caption.get("style_description")
    if not isinstance(style, dict):
        raise ValueError("Ideogram 4 caption requires style_description")
    has_photo = "photo" in style
    has_art = "art_style" in style
    if has_photo == has_art:
        raise ValueError("Ideogram style must contain exactly one of photo or art_style")

    return raw.strip()


def _normalize_hex_color(value: str) -> str:
    normalized = value.strip().upper()
    if len(normalized) != 7 or not normalized.startswith("#"):
        raise ValueError("Ideogram colors must use #RRGGBB hexadecimal notation")
    if any(character not in "0123456789ABCDEF" for character in normalized[1:]):
        raise ValueError("Ideogram colors must use #RRGGBB hexadecimal notation")
    return normalized
