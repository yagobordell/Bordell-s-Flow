"""Strict source-preserving interfaces for the B1.1 / B1.2 / B2 bots.

Models describe the exact JSON object on the wire. API Structured Outputs already
provide JSON transport, so the Markdown code fences requested by the human-readable
prompt files are intentionally not part of the parsed response.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BlockType = Literal["intro", "development", "pivot", "correction", "climax", "close"]
BeatType = Literal[
    "orientation", "setup", "action", "reaction", "claim", "evidence", "data",
    "causal_link", "consequence", "escalation", "contrast", "analogy", "revelation",
    "pivot", "correction", "question", "verdict", "return", "transition",
]
VisualType = Literal["avatar", "avatar_media", "media_image", "media_video"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NarrativeCore(StrictModel):
    central_question: str = Field(min_length=1)
    final_answer: str = Field(min_length=1)


class Span(StrictModel):
    first_words: str = Field(min_length=1)
    last_words: str = Field(min_length=1)


class BlockMeta(StrictModel):
    block_id: int = Field(ge=1)
    type: BlockType
    emotional_entry: str = Field(min_length=1)
    emotional_exit: str = Field(min_length=1)
    span: Span


class B11Error(StrictModel):
    stage: Literal["B1.1"]
    code: Literal["UNSUPPORTED_INPUT", "AMBIGUOUS_SOURCE"]
    message: str = Field(min_length=1)


class B11Output(StrictModel):
    pipeline_stage: Literal["B1.1"] | None
    narrative_core: NarrativeCore | None
    blocks: list[BlockMeta] | None
    error: B11Error | None

    @model_validator(mode="after")
    def valid_success_or_error(self) -> "B11Output":
        if self.error is None:
            if self.pipeline_stage != "B1.1" or self.narrative_core is None or not self.blocks:
                raise ValueError("B1.1 success must have a core and non-empty blocks")
        elif any(v is not None for v in (self.pipeline_stage, self.narrative_core, self.blocks)):
            raise ValueError("B1.1 error must null out success fields")
        return self


class MaterializedBlock(StrictModel):
    block_id: int = Field(ge=1)
    type: BlockType
    emotional_entry: str = Field(min_length=1)
    emotional_exit: str = Field(min_length=1)
    text: str = Field(min_length=1)


class B12Input(StrictModel):
    narrative_core: NarrativeCore
    current_block_id: int = Field(ge=1)
    blocks: tuple[MaterializedBlock] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def matching_block(self) -> "B12Input":
        if self.blocks[0].block_id != self.current_block_id or not self.blocks[0].text.strip():
            raise ValueError("B1.2 must receive only its own nonempty materialized block")
        return self


class Beat(StrictModel):
    beat_id: str = Field(min_length=2)
    text: str = Field(min_length=1)
    beat_type: BeatType


class B12Error(StrictModel):
    stage: Literal["B1.2"]
    code: Literal["INVALID_B1_INPUT"]
    message: str = Field(min_length=1)


class B12Output(StrictModel):
    pipeline_stage: Literal["B1.2"] | None
    block_id: int | None
    beats: list[Beat] | None
    error: B12Error | None

    @model_validator(mode="after")
    def valid_success_or_error(self) -> "B12Output":
        if self.error is None:
            if self.pipeline_stage != "B1.2" or self.block_id is None or not self.beats:
                raise ValueError("B1.2 success must have a block ID and non-empty beats")
        elif any(v is not None for v in (self.pipeline_stage, self.block_id, self.beats)):
            raise ValueError("B1.2 error must null out success fields")
        return self


class B2InputBlock(StrictModel):
    block_id: int = Field(ge=1)
    type: BlockType
    emotional_entry: str = Field(min_length=1)
    emotional_exit: str = Field(min_length=1)
    span: Span
    beats: list[Beat] = Field(min_length=1)


class B2Input(StrictModel):
    narrative_core: NarrativeCore
    current_block_id: int = Field(ge=1)
    blocks: tuple[B2InputBlock] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def matching_block(self) -> "B2Input":
        if self.blocks[0].block_id != self.current_block_id:
            raise ValueError("B2 must receive only its own block")
        return self


class VisualBeat(Beat):
    visual_type: VisualType
    description: str | None

    @model_validator(mode="after")
    def description_matches_visual(self) -> "VisualBeat":
        if self.visual_type == "avatar":
            if self.description is not None:
                raise ValueError("avatar must have native null description")
        elif not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("non-avatar visual requires a nonempty description")
        return self


class B2Block(B2InputBlock):
    beats: list[VisualBeat] = Field(min_length=1)


class B2Error(StrictModel):
    stage: Literal["B2"]
    code: Literal["INVALID_UPSTREAM_INPUT"]
    message: str = Field(min_length=1)


class B2Output(StrictModel):
    pipeline_stage: Literal["B2"] | None
    narrative_core: NarrativeCore | None
    blocks: list[B2Block] | None
    error: B2Error | None

    @model_validator(mode="after")
    def valid_success_or_error(self) -> "B2Output":
        if self.error is None:
            if self.pipeline_stage != "B2" or self.narrative_core is None or not self.blocks:
                raise ValueError("B2 success must have a core and one block")
            if len(self.blocks) != 1:
                raise ValueError("B2 output must contain exactly one block")
        elif any(v is not None for v in (self.pipeline_stage, self.narrative_core, self.blocks)):
            raise ValueError("B2 error must null out success fields")
        return self
