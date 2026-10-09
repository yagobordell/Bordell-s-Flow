"""Structured outputs for the professional ebook CTA detector and visual director."""

from typing import Literal

from pydantic import Field, model_validator

from ai_video_factory.bots.contracts import ProfessionalVisualBeat, StrictModel


class ProfessionalCTADetection(StrictModel):
    """Chronologically ordered complete-beat ebook promotion groups."""

    cta_groups: list[list[str]]


class ProfessionalCTAVisualBeat(StrictModel):
    beat_id: str = Field(min_length=2)
    visual_type: Literal["avatar", "avatar_media", "media_image", "media_video"]
    prompt: str | None
    video_prompt: str | None
    reference: bool
    audio: bool
    reference_sources: list[Literal["presenter", "ebook"]]

    @model_validator(mode="after")
    def validate_visual(self) -> "ProfessionalCTAVisualBeat":
        if len(self.reference_sources) != len(set(self.reference_sources)):
            raise ValueError("CTA references cannot be duplicated")
        if self.reference != bool(self.reference_sources):
            raise ValueError("CTA reference must match selected image sources")
        if self.visual_type == "avatar" and self.prompt is None:
            if self.reference_sources:
                raise ValueError("Reused avatar must not regenerate an image")
        if self.audio and self.visual_type == "media_video":
            if "presenter" not in self.reference_sources:
                raise ValueError("Speaking CTA media_video must reference the real presenter")
        # Delegate all ordinary visual/audio/video constraints to B2 Professional.
        ProfessionalVisualBeat.model_validate({
            "beat_id": self.beat_id,
            "text": "Source text supplied in immutable B1.2",
            "beat_type": "claim",
            "visual_type": self.visual_type,
            "prompt": self.prompt,
            "video_prompt": self.video_prompt,
            "reference": self.reference,
            "audio": self.audio,
        })
        return self


class ProfessionalCTAVisuals(StrictModel):
    beats: list[ProfessionalCTAVisualBeat] = Field(min_length=1)
