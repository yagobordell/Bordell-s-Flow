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
    def validate_interval(self) -> "CompositionShot":
        if self.end_frame <= self.start_frame:
            raise ValueError("Composition shot end_frame must be greater than start_frame")
        if self.duration_frames != self.end_frame - self.start_frame:
            raise ValueError("Composition shot duration_frames must match its frame interval")
        return self


class CompositionPlan(BaseModel):
    """Deterministic Phase 9 visual timeline consumed by later rendering stages."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = Field(gt=0)
    total_frames: int = Field(gt=0)
    shots: list[CompositionShot] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_timeline(self) -> "CompositionPlan":
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

        return self
