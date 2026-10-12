"""Strict source-preserving interfaces for the B1.1 / B1.2 / B2 / B2.1 bots.

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


class ProfessionalVisualBeat(Beat):
    """Professional B2 has separate image/video prompts and speaker flags."""

    visual_type: VisualType
    prompt: str | None
    video_prompt: str | None
    reference: bool
    audio: bool

    @model_validator(mode="after")
    def valid_professional_visual(self) -> "ProfessionalVisualBeat":
        if self.prompt is None:
            if self.visual_type != "avatar" or self.reference:
                raise ValueError("Only a reused avatar can have a null prompt and no reference")
        elif not self.prompt.strip():
            raise ValueError("Professional image prompts must be nonempty")
        if self.visual_type == "media_video":
            if not isinstance(self.video_prompt, str) or not self.video_prompt.strip():
                raise ValueError("media_video requires a nonempty video_prompt")
        elif self.video_prompt is not None:
            raise ValueError("video_prompt must be null unless visual_type is media_video")
        if self.visual_type in {"avatar", "avatar_media"} and not self.audio:
            raise ValueError("Presenter visuals must have audio=true")
        if self.visual_type in {"media_image", "media_video"} and self.audio:
            raise ValueError("Only avatar and avatar_media may have audio=true")
        if self.visual_type == "avatar" and self.prompt is not None:
            raise ValueError("Talking avatar must reuse its original image")
        return self


class ProfessionalB2Block(B2InputBlock):
    beats: list[ProfessionalVisualBeat] = Field(min_length=1)

    @model_validator(mode="after")
    def mandatory_close_anchor(self) -> "ProfessionalB2Block":
        """The closing anchor beat must reuse the original avatar image."""

        def is_base_avatar(beat: ProfessionalVisualBeat) -> bool:
            return (
                beat.visual_type == "avatar"
                and beat.prompt is None
                and beat.video_prompt is None
                and not beat.reference
                and beat.audio
            )

        if self.type == "close" and not is_base_avatar(self.beats[-1]):
            raise ValueError(
                "Professional close last beat must reuse the original avatar: "
                "visual_type=avatar, prompt=null, video_prompt=null, "
                "reference=false, audio=true"
            )
        return self


class ProfessionalB2Output(StrictModel):
    pipeline_stage: Literal["B2"] | None
    narrative_core: NarrativeCore | None
    blocks: list[ProfessionalB2Block] | None
    error: B2Error | None

    @model_validator(mode="after")
    def valid_success_or_error(self) -> "ProfessionalB2Output":
        if self.error is None:
            if self.pipeline_stage != "B2" or self.narrative_core is None or not self.blocks:
                raise ValueError("Professional B2 success requires a core and a block")
            if len(self.blocks) != 1:
                raise ValueError("Professional B2 output must contain exactly one block")
        elif any(v is not None for v in (self.pipeline_stage, self.narrative_core, self.blocks)):
            raise ValueError("Professional B2 error must null out success fields")
        return self


B21SplitMode = Literal["grid", "independent"]


class B21BeatPatch(StrictModel):
    """Visual-only replacement for one professional B2 beat."""

    block_id: int = Field(ge=1)
    beat_id: str = Field(min_length=2)
    prompt: str = Field(min_length=1)
    video_prompt: str | None
    second_prompt: str | None
    split_mode: B21SplitMode | None

    @model_validator(mode="after")
    def valid_patch_shape(self) -> "B21BeatPatch":
        if not self.prompt.strip():
            raise ValueError("B2.1 prompt must be nonempty")
        if self.video_prompt is not None and not self.video_prompt.strip():
            raise ValueError("B2.1 video_prompt must be null or nonempty")
        if self.second_prompt is None:
            if self.split_mode is not None:
                raise ValueError("B2.1 split_mode requires second_prompt")
        else:
            if not self.second_prompt.strip():
                raise ValueError("B2.1 second_prompt must be nonempty")
            if self.split_mode is None:
                raise ValueError("B2.1 second_prompt requires split_mode")
            if self.second_prompt.strip() == self.prompt.strip():
                raise ValueError("B2.1 split prompts cannot be textually identical")
        return self


class B21Error(StrictModel):
    stage: Literal["B2.1"]
    code: Literal["INVALID_UPSTREAM_INPUT"]
    message: str = Field(min_length=1)


class B21Output(StrictModel):
    """Whole-video visual review. Only beats that change are emitted."""

    pipeline_stage: Literal["B2.1"] | None
    modified_beats: list[B21BeatPatch] | None
    error: B21Error | None

    @model_validator(mode="after")
    def valid_success_or_error(self) -> "B21Output":
        if self.error is None:
            if self.pipeline_stage != "B2.1" or self.modified_beats is None:
                raise ValueError("B2.1 success requires a modified_beats list")
        elif self.pipeline_stage is not None or self.modified_beats is not None:
            raise ValueError("B2.1 error must null out success fields")
        return self


class WholeB12Input(StrictModel):
    """One B1.2 call receives the complete immutable narration and all blocks."""
    plain_script_for_recording: str = Field(min_length=1)
    narrative_core: NarrativeCore
    blocks: list[MaterializedBlock] = Field(min_length=1)


class WholeB12Block(StrictModel):
    block_id: int = Field(ge=1)
    beats: list[Beat] = Field(min_length=1)


class WholeB12Output(StrictModel):
    pipeline_stage: Literal["B1.2"] | None
    narrative_core: NarrativeCore | None
    blocks: list[WholeB12Block] | None
    error: B12Error | None

    @model_validator(mode="after")
    def valid_success_or_error(self) -> "WholeB12Output":
        if self.error is None:
            if self.pipeline_stage != "B1.2" or self.narrative_core is None or not self.blocks:
                raise ValueError("Whole B1.2 success requires core and all blocks")
        elif any(v is not None for v in (self.pipeline_stage, self.narrative_core, self.blocks)):
            raise ValueError("Whole B1.2 error must null out success fields")
        return self


class WholeProfessionalB2Input(StrictModel):
    """Professional video director receives every frozen block and beat."""
    narrative_core: NarrativeCore
    blocks: list[B2InputBlock] = Field(min_length=1)
    cta_groups: list[list[str]] | None = None


class ProfessionalB2BatchInput(StrictModel):
    """One sequential batch of up to four frozen source blocks."""
    narrative_core: NarrativeCore
    batch_number: int = Field(ge=1)
    total_batches: int = Field(ge=1)
    blocks: list[B2InputBlock] = Field(min_length=1, max_length=4)
    cta_groups: list[list[str]] | None = None

    @model_validator(mode="after")
    def batch_within_run(self) -> "ProfessionalB2BatchInput":
        if self.batch_number > self.total_batches:
            raise ValueError("B2 batch_number cannot exceed total_batches")
        return self


class WholeProfessionalB2Output(StrictModel):
    pipeline_stage: Literal["B2"] | None
    narrative_core: NarrativeCore | None
    blocks: list[ProfessionalB2Block] | None
    error: B2Error | None

    @model_validator(mode="after")
    def valid_success_or_error(self) -> "WholeProfessionalB2Output":
        if self.error is None:
            if self.pipeline_stage != "B2" or self.narrative_core is None or not self.blocks:
                raise ValueError("Whole professional B2 success requires core and all blocks")
        elif any(v is not None for v in (self.pipeline_stage, self.narrative_core, self.blocks)):
            raise ValueError("Whole professional B2 error must null out success fields")
        return self


class ProfessionalB2CorrectionBlock(StrictModel):
    """Only the named beats in one failed B2 validation group."""
    block_id: int = Field(ge=1)
    beats: list[ProfessionalVisualBeat] = Field(min_length=1)


class ProfessionalB2CorrectionOutput(StrictModel):
    """Sparse B2 correction response; unaffected beats are merged locally."""
    pipeline_stage: Literal["B2"] | None
    narrative_core: NarrativeCore | None
    blocks: list[ProfessionalB2CorrectionBlock] | None
    error: B2Error | None

    @model_validator(mode="after")
    def valid_success_or_error(self) -> "ProfessionalB2CorrectionOutput":
        if self.error is None:
            if self.pipeline_stage != "B2" or self.narrative_core is None or not self.blocks:
                raise ValueError("B2 targeted correction requires core and beat groups")
        elif any(v is not None for v in (self.pipeline_stage, self.narrative_core, self.blocks)):
            raise ValueError("B2 correction error must null out success fields")
        return self


class ProfessionalB2BatchOutput(StrictModel):
    """Validated visual output for the current B2 block batch only."""
    pipeline_stage: Literal["B2"] | None
    narrative_core: NarrativeCore | None
    blocks: list[ProfessionalB2Block] | None
    error: B2Error | None

    @model_validator(mode="after")
    def valid_success_or_error(self) -> "ProfessionalB2BatchOutput":
        if self.error is None:
            if self.pipeline_stage != "B2" or self.narrative_core is None or not self.blocks:
                raise ValueError("B2 batch output requires core and one or more blocks")
            if len(self.blocks) > 4:
                raise ValueError("B2 batch output cannot exceed four blocks")
        elif any(v is not None for v in (self.pipeline_stage, self.narrative_core, self.blocks)):
            raise ValueError("B2 batch error must null out success fields")
        return self
