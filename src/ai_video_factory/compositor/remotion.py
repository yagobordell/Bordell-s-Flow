from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from math import floor, isclose
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .media import MediaProbe, probe_video
from .models import CaptionCue, CaptionWord, CompositionPlan

_DAGGER = "\u2020"
_DAGGER_RULE = "remove_unicode_dagger_u2020"
ProbeVideo = Callable[[Path], MediaProbe]


class RemotionShot(BaseModel):
    """One staged source clip consumed by the local Remotion renderer."""

    model_config = ConfigDict(extra="forbid")

    shot_id: int = Field(ge=1)
    src: str = Field(min_length=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    duration_frames: int = Field(gt=0)


class RemotionCaptionWord(BaseModel):
    """Presentation text and timing for one caption word."""

    model_config = ConfigDict(extra="forbid")

    word_id: int = Field(ge=1)
    text: str = Field(min_length=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)


class RemotionCaptionCue(BaseModel):
    """Presentation-ready caption cue passed to Remotion."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    text: str = Field(min_length=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    words: list[RemotionCaptionWord] = Field(min_length=1)


class PresentationNormalization(BaseModel):
    """Auditable presentation-only text cleanup applied after Phase 9.2."""

    model_config = ConfigDict(extra="forbid")

    word_id: int = Field(ge=1)
    source_text: str = Field(min_length=1)
    display_text: str = Field(min_length=1)
    rule: Literal["remove_unicode_dagger_u2020"] = _DAGGER_RULE


class RemotionVisualProfile(BaseModel):
    """Renderer-only Phase 9.4 motion settings that cannot change canonical timing."""

    model_config = ConfigDict(extra="forbid")

    transition_frames: int = Field(default=6, ge=0)
    transition_floor_opacity: float = Field(default=0.72, ge=0.0, le=1.0)
    transition_scale: float = Field(default=1.015, ge=1.0, le=1.1)
    caption_motion_frames: int = Field(default=4, ge=0)
    boundary_accent_frames: int = Field(default=5, ge=0)
    show_progress_bar: bool = True


class RemotionRenderProps(BaseModel):
    """Renderer-only props contract generated from a validated composition plan."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"] = "2"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = Field(gt=0)
    total_frames: int = Field(gt=0)
    shots: list[RemotionShot] = Field(min_length=1)
    captions: list[RemotionCaptionCue] = Field(default_factory=list)
    presentation_normalizations: list[PresentationNormalization] = Field(default_factory=list)
    visual_profile: RemotionVisualProfile = Field(default_factory=RemotionVisualProfile)

    @model_validator(mode="after")
    def validate_timeline(self) -> RemotionRenderProps:
        shot_ids = [shot.shot_id for shot in self.shots]
        if shot_ids != list(range(1, len(self.shots) + 1)):
            raise ValueError("Remotion shots must have consecutive IDs starting at 1")
        if self.shots[0].start_frame != 0:
            raise ValueError("Remotion shot timeline must start at frame 0")

        for shot in self.shots:
            if shot.duration_frames != shot.end_frame - shot.start_frame:
                raise ValueError("Remotion shot duration must match its frame interval")
            if not shot.src.startswith("/media/"):
                raise ValueError("Remotion shot sources must resolve below /media/")
            # The renderer derives an effective window per shot. Very short shots
            # remain valid timeline inputs; their visual windows are clamped to
            # the frames available inside that shot instead of changing timing.

        for previous, current in zip(self.shots, self.shots[1:], strict=False):
            if previous.end_frame != current.start_frame:
                raise ValueError("Remotion shots must be contiguous")
        if self.shots[-1].end_frame != self.total_frames:
            raise ValueError("Remotion total_frames must match the final shot boundary")

        _validate_remotion_captions(self.captions, total_frames=self.total_frames)
        _validate_normalizations(self.presentation_normalizations, self.captions)
        return self


def prepare_remotion_props(
    plan: CompositionPlan,
    *,
    public_dir: Path,
    visual_profile: RemotionVisualProfile | None = None,
) -> RemotionRenderProps:
    """Stage local shot media and build deterministic presentation-ready Remotion props."""

    media_dir = public_dir.resolve() / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    shots: list[RemotionShot] = []
    expected_media_names: set[str] = set()
    for shot in plan.shots:
        source = Path(shot.uri).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Composition source clip not found: {source}")
        if source.suffix.lower() != ".mp4":
            raise ValueError(f"Remotion source clip must be an MP4: {source}")

        media_name = f"shot_{shot.shot_id:03d}.mp4"
        expected_media_names.add(media_name)
        target = media_dir / media_name
        _stage_media(source, target)
        shots.append(
            RemotionShot(
                shot_id=shot.shot_id,
                src=f"/media/{media_name}",
                start_frame=shot.start_frame,
                end_frame=shot.end_frame,
                duration_frames=shot.duration_frames,
            )
        )

    _remove_stale_media(media_dir, expected_media_names)

    captions: list[RemotionCaptionCue] = []
    normalizations: list[PresentationNormalization] = []
    for cue in plan.captions:
        render_cue, cue_normalizations = _prepare_caption(cue)
        captions.append(render_cue)
        normalizations.extend(cue_normalizations)

    return RemotionRenderProps(
        width=plan.width,
        height=plan.height,
        fps=plan.fps,
        total_frames=plan.total_frames,
        shots=shots,
        captions=captions,
        presentation_normalizations=normalizations,
        visual_profile=visual_profile or RemotionVisualProfile(),
    )


def normalize_caption_display_text(text: str) -> str:
    """Remove the known U+2020 transcription artifact from renderer display text only."""

    cleaned = text.replace(_DAGGER, "")
    if not cleaned.strip():
        return text
    return cleaned


def validate_remotion_visual(
    path: Path,
    props: RemotionRenderProps,
    *,
    probe: ProbeVideo = probe_video,
) -> MediaProbe:
    """Validate a silent Remotion render before later narration muxing."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Remotion render not found: {resolved}")

    media = probe(resolved)
    if media.codec_name != "h264":
        raise ValueError(f"Remotion render must use H.264, found codec={media.codec_name}")
    if (media.width, media.height) != (props.width, props.height):
        raise ValueError(
            "Remotion render dimensions do not match the composition plan: "
            f"expected={props.width}x{props.height}, found={media.width}x{media.height}"
        )
    if not isclose(media.fps, float(props.fps), rel_tol=0.0, abs_tol=1e-6):
        raise ValueError(f"Remotion render must be {props.fps} fps, found {media.fps:g}")
    if media.audio_stream_count != 0:
        raise ValueError("Phase 9 Remotion visual render must remain silent")

    frame_count = media.frame_count
    if frame_count is None:
        frame_count = floor(media.duration_seconds * props.fps + 0.5)
    if frame_count != props.total_frames:
        raise ValueError(
            "Remotion render frame count does not match the canonical timeline: "
            f"expected={props.total_frames}, found={frame_count}"
        )
    return media


def _prepare_caption(
    cue: CaptionCue,
) -> tuple[RemotionCaptionCue, list[PresentationNormalization]]:
    words: list[RemotionCaptionWord] = []
    normalizations: list[PresentationNormalization] = []

    for word in cue.words:
        display_text = normalize_caption_display_text(word.text)
        if display_text != word.text:
            normalizations.append(
                PresentationNormalization(
                    word_id=word.word_id,
                    source_text=word.text,
                    display_text=display_text,
                )
            )
        words.append(_render_word(word, display_text=display_text))

    return (
        RemotionCaptionCue(
            id=cue.id,
            text=" ".join(word.text for word in words),
            start_frame=cue.start_frame,
            end_frame=cue.end_frame,
            words=words,
        ),
        normalizations,
    )


def _render_word(word: CaptionWord, *, display_text: str) -> RemotionCaptionWord:
    return RemotionCaptionWord(
        word_id=word.word_id,
        text=display_text,
        start_frame=word.start_frame,
        end_frame=word.end_frame,
    )


def _stage_media(source: Path, target: Path) -> None:
    if target.is_file():
        try:
            if os.path.samefile(source, target):
                return
        except OSError:
            pass
        if source.stat().st_size == target.stat().st_size:
            if _sha256(source) == _sha256(target):
                return
        target.unlink()
    elif target.exists():
        raise ValueError(f"Remotion media target is not a regular file: {target}")

    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _remove_stale_media(media_dir: Path, expected_names: set[str]) -> None:
    for path in media_dir.glob("shot_*.mp4"):
        if path.name not in expected_names:
            path.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_remotion_captions(
    captions: list[RemotionCaptionCue],
    *,
    total_frames: int,
) -> None:
    if not captions:
        return

    caption_ids = [caption.id for caption in captions]
    if caption_ids != list(range(1, len(captions) + 1)):
        raise ValueError("Remotion caption IDs must be consecutive starting at 1")

    word_ids = [word.word_id for caption in captions for word in caption.words]
    if word_ids != list(range(1, len(word_ids) + 1)):
        raise ValueError("Remotion captions must cover narration word IDs exactly once")

    for caption in captions:
        if caption.start_frame != caption.words[0].start_frame:
            raise ValueError("Remotion caption start must match its first word")
        if caption.end_frame != caption.words[-1].end_frame:
            raise ValueError("Remotion caption end must match its final word")
        if caption.end_frame > total_frames:
            raise ValueError("Remotion captions must stay inside the composition timeline")
        if caption.text != " ".join(word.text for word in caption.words):
            raise ValueError("Remotion caption text must be reconstructed from its words")

        for previous, current in zip(caption.words, caption.words[1:], strict=False):
            if current.start_frame < previous.end_frame:
                raise ValueError("Remotion caption words must not overlap")

    for previous, current in zip(captions, captions[1:], strict=False):
        if current.start_frame < previous.end_frame:
            raise ValueError("Remotion caption cues must not overlap")


def _validate_normalizations(
    normalizations: list[PresentationNormalization],
    captions: list[RemotionCaptionCue],
) -> None:
    if len({item.word_id for item in normalizations}) != len(normalizations):
        raise ValueError("Presentation normalizations must reference unique word IDs")

    display_words = {word.word_id: word.text for caption in captions for word in caption.words}
    for item in normalizations:
        if item.source_text == item.display_text:
            raise ValueError("Presentation normalization must change display text")
        if display_words.get(item.word_id) != item.display_text:
            raise ValueError("Presentation normalization must match renderer caption text")
