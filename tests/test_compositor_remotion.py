from pathlib import Path

import pytest

from ai_video_factory.compositor import CaptionCue, CaptionWord, CompositionPlan, CompositionShot
from ai_video_factory.compositor.media import MediaProbe
from ai_video_factory.compositor.remotion import (
    normalize_caption_display_text,
    prepare_remotion_props,
    validate_remotion_visual,
)


def _plan(tmp_path: Path) -> CompositionPlan:
    sources = tmp_path / "sources"
    sources.mkdir()
    first = sources / "shot_001.mp4"
    second = sources / "shot_002.mp4"
    first.write_bytes(b"first-video")
    second.write_bytes(b"second-video")

    return CompositionPlan(
        width=768,
        height=1280,
        fps=24,
        total_frames=48,
        shots=[
            CompositionShot(
                shot_id=1,
                uri=first.as_posix(),
                start_frame=0,
                end_frame=24,
                duration_frames=24,
                source_duration_seconds=1.1,
                source_frame_count=27,
            ),
            CompositionShot(
                shot_id=2,
                uri=second.as_posix(),
                start_frame=24,
                end_frame=48,
                duration_frames=24,
                source_duration_seconds=1.1,
                source_frame_count=27,
            ),
        ],
        captions=[
            CaptionCue(
                id=1,
                text="significa †el que sirve†",
                start_frame=4,
                end_frame=20,
                word_ids=[1, 2, 3, 4],
                words=[
                    CaptionWord(word_id=1, text="significa", start_frame=4, end_frame=8),
                    CaptionWord(word_id=2, text="†el", start_frame=8, end_frame=12),
                    CaptionWord(word_id=3, text="que", start_frame=12, end_frame=16),
                    CaptionWord(word_id=4, text="sirve†", start_frame=16, end_frame=20),
                ],
            )
        ],
    )


def test_prepare_remotion_props_stages_media_and_normalizes_daggers(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    public_dir = tmp_path / "remotion" / "public"

    props = prepare_remotion_props(plan, public_dir=public_dir)

    assert [shot.src for shot in props.shots] == [
        "/media/shot_001.mp4",
        "/media/shot_002.mp4",
    ]
    assert (public_dir / "media" / "shot_001.mp4").read_bytes() == b"first-video"
    assert (public_dir / "media" / "shot_002.mp4").read_bytes() == b"second-video"
    assert props.captions[0].text == "significa el que sirve"
    assert [word.text for word in props.captions[0].words] == [
        "significa",
        "el",
        "que",
        "sirve",
    ]
    assert [item.model_dump() for item in props.presentation_normalizations] == [
        {
            "word_id": 2,
            "source_text": "†el",
            "display_text": "el",
            "rule": "remove_unicode_dagger_u2020",
        },
        {
            "word_id": 4,
            "source_text": "sirve†",
            "display_text": "sirve",
            "rule": "remove_unicode_dagger_u2020",
        },
    ]
    assert plan.captions[0].text == "significa †el que sirve†"


def test_prepare_remotion_props_removes_stale_staged_clip(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    media_dir = tmp_path / "remotion" / "public" / "media"
    media_dir.mkdir(parents=True)
    stale = media_dir / "shot_999.mp4"
    stale.write_bytes(b"stale")

    prepare_remotion_props(plan, public_dir=media_dir.parent)

    assert not stale.exists()


def test_normalization_keeps_non_dagger_text_and_avoids_empty_output() -> None:
    assert normalize_caption_display_text("samuráis") == "samuráis"
    assert normalize_caption_display_text("†") == "†"


def test_validate_remotion_visual_accepts_exact_silent_render(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    props = prepare_remotion_props(plan, public_dir=tmp_path / "public")
    output = tmp_path / "visual.mp4"
    output.touch()

    def probe(_: Path) -> MediaProbe:
        return MediaProbe(
            codec_name="h264",
            width=768,
            height=1280,
            fps=24.0,
            duration_seconds=2.0,
            frame_count=48,
            audio_stream_count=0,
        )

    media = validate_remotion_visual(output, props, probe=probe)

    assert media.frame_count == 48


def test_validate_remotion_visual_rejects_audio_or_wrong_frame_count(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    props = prepare_remotion_props(plan, public_dir=tmp_path / "public")
    output = tmp_path / "visual.mp4"
    output.touch()

    def probe_with_audio(_: Path) -> MediaProbe:
        return MediaProbe(
            codec_name="h264",
            width=768,
            height=1280,
            fps=24.0,
            duration_seconds=2.0,
            frame_count=48,
            audio_stream_count=1,
        )

    with pytest.raises(ValueError, match="must remain silent"):
        validate_remotion_visual(output, props, probe=probe_with_audio)

    def probe_short(_: Path) -> MediaProbe:
        return MediaProbe(
            codec_name="h264",
            width=768,
            height=1280,
            fps=24.0,
            duration_seconds=47 / 24,
            frame_count=47,
            audio_stream_count=0,
        )

    with pytest.raises(ValueError, match="frame count"):
        validate_remotion_visual(output, props, probe=probe_short)
