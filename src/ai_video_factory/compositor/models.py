from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompositionShot(BaseModel):
    """Frame-exact placement of one source video clip on the canonical timeline."""

    model_config = ConfigDict(extra="forbid")

    shot_id: int = Field(ge=1)
    uri: str = Field(min_length=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    duration_frames: int = Field(gt=0)
    source_duration_seconds: float = Field(gt=0)
    source_frame_count: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> CompositionShot:
        if self.end_frame <= self.start_frame:
            raise ValueError("Composition shot end_frame must be greater than start_frame")
        if self.duration_frames != self.end_frame - self.start_frame:
            raise ValueError("Composition shot duration_frames must match its frame interval")
        return self


class CaptionWord(BaseModel):
    """One canonical narration word quantized onto the composition frame timeline."""

    model_config = ConfigDict(extra="forbid")

    word_id: int = Field(ge=1)
    text: str = Field(min_length=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> CaptionWord:
        if self.end_frame <= self.start_frame:
            raise ValueError("Caption word end_frame must be greater than start_frame")
        return self


class CaptionCue(BaseModel):
    """A short display group of ordered narration words for the caption renderer."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    text: str = Field(min_length=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    word_ids: list[int] = Field(min_length=1)
    words: list[CaptionWord] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_words(self) -> CaptionCue:
        if self.end_frame <= self.start_frame:
            raise ValueError("Caption cue end_frame must be greater than start_frame")
        if self.word_ids != [word.word_id for word in self.words]:
            raise ValueError("Caption cue word_ids must match its ordered words")
        if self.text != " ".join(word.text for word in self.words):
            raise ValueError("Caption cue text must be reconstructed from its words")
        if self.start_frame != self.words[0].start_frame:
            raise ValueError("Caption cue start_frame must match its first word")
        if self.end_frame != self.words[-1].end_frame:
            raise ValueError("Caption cue end_frame must match its final word")

        for previous, current in zip(self.words, self.words[1:], strict=False):
            if current.start_frame < previous.end_frame:
                raise ValueError("Caption words must not overlap")
        return self


class CompositionPlan(BaseModel):
    """Deterministic Phase 9 timeline consumed by later rendering stages."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = Field(gt=0)
    total_frames: int = Field(gt=0)
    shots: list[CompositionShot] = Field(min_length=1)
    captions: list[CaptionCue] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_timeline(self) -> CompositionPlan:
        shot_ids = [shot.shot_id for shot in self.shots]
        if shot_ids != list(range(1, len(self.shots) + 1)):
            raise ValueError("Composition shots must have consecutive IDs starting at 1")

        if self.shots[0].start_frame != 0:
            raise ValueError("Composition timeline must start at frame 0")

        for previous, current in zip(self.shots, self.shots[1:], strict=False):
            if previous.end_frame != current.start_frame:
                raise ValueError("Composition shots must be contiguous without gaps or overlaps")

        if self.shots[-1].end_frame != self.total_frames:
            raise ValueError("Composition total_frames must equal the final shot boundary")

        if self.captions:
            caption_ids = [caption.id for caption in self.captions]
            if caption_ids != list(range(1, len(self.captions) + 1)):
                raise ValueError("Caption cues must have consecutive IDs starting at 1")

            word_ids = [word_id for caption in self.captions for word_id in caption.word_ids]
            if word_ids != list(range(1, len(word_ids) + 1)):
                raise ValueError(
                    "Caption cues must cover consecutive narration word IDs exactly once"
                )

            for caption in self.captions:
                if (
                    caption.start_frame >= self.total_frames
                    or caption.end_frame > self.total_frames
                ):
                    raise ValueError("Caption cues must remain inside the composition timeline")

            for previous, current in zip(self.captions, self.captions[1:], strict=False):
                if current.start_frame < previous.end_frame:
                    raise ValueError("Caption cues must preserve order without overlaps")

        return self
