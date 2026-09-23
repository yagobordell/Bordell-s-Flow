from typing import Literal

from pydantic import BaseModel, Field


class SourceScript(BaseModel):
    """Canonical input for the production pipeline."""

    text: str = Field(min_length=1)

class NarrativeBlock(BaseModel):
    """A contiguous semantic unit of the source script."""

    id: int = Field(ge=1)
    text: str = Field(min_length=1)

class Beat(BaseModel):
    """A single visualizable action, change, or idea inside a narrative block."""

    id: int = Field(ge=1)
    block_id: int = Field(ge=1)
    action: str = Field(min_length=1)

class Scene(BaseModel):
    """Minimal scene contract: an ordered grouping of narrative beats."""

    id: int = Field(ge=1)
    beat_ids: list[int] = Field(min_length=1)

class ContinuityEntity(BaseModel):
    """Canonical recurring visual entity tracked across narrative blocks."""

    id: str = Field(pattern=r"^(character|group|location|object)_\d{3,}$")
    kind: Literal["character", "group", "location", "object"]
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)

class BlockContinuity(BaseModel):
    """Entity references that must remain consistent inside one narrative block."""

    block_id: int = Field(ge=1)
    entity_ids: list[str] = Field(default_factory=list)

class Shot(BaseModel):
    """Minimal audiovisual unit planned from consecutive beats inside one scene."""

    id: int = Field(ge=1)
    scene_id: int = Field(ge=1)
    beat_ids: list[int] = Field(min_length=1)
    entity_ids: list[str] = Field(default_factory=list)
    action: str = Field(min_length=1)

class VisualReference(BaseModel):
    """Canonical provider-neutral visual reference prompt for one continuity entity."""

    entity_id: str = Field(pattern=r"^(character|group|location|object)_\d{3,}$")
    prompt: str = Field(min_length=1)

class ReferenceAsset(BaseModel):
    """Persisted reference-image asset bound to one canonical continuity entity."""

    entity_id: str = Field(pattern=r"^(character|group|location|object)_\d{3,}$")
    uri: str = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)

class NarrationAudio(BaseModel):
    """Canonical narration audio asset and its measured playback duration."""

    uri: str = Field(min_length=1)
    duration_seconds: float = Field(gt=0)

class NarrationWord(BaseModel):
    """Recognized narration word used as timing evidence for later alignment."""

    id: int = Field(ge=1)
    text: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)

class BeatTiming(BaseModel):
    """Canonical mapping from one narrative beat to a contiguous narration interval."""

    beat_id: int = Field(ge=1)
    start_word_id: int = Field(ge=1)
    end_word_id: int = Field(ge=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)

class ShotTiming(BaseModel):
    """Canonical narration interval occupied by one planned shot."""

    shot_id: int = Field(ge=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)

class StoryboardFrame(BaseModel):
    """Provider-neutral still-image prompt for one canonical planned shot."""

    shot_id: int = Field(ge=1)
    prompt: str = Field(min_length=1)

class StoryboardKeyframe(BaseModel):
    """Persisted storyboard still-image asset bound to one canonical shot."""

    shot_id: int = Field(ge=1)
    uri: str = Field(min_length=1)

class VideoPrompt(BaseModel):
    """Provider-neutral temporal motion prompt for one canonical planned shot."""

    shot_id: int = Field(ge=1)
    prompt: str = Field(min_length=1)

