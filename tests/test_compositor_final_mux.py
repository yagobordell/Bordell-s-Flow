import wave
from pathlib import Path

import pytest

from ai_video_factory.compositor import (
    AudioStreamProbe,
    CompositionPlan,
    CompositionShot,
    build_final_mux_command,
    parse_audio_stream_payload,
    probe_wav,
    validate_final_mux_inputs,
    validate_final_mux_output,
)
from ai_video_factory.compositor.media import MediaProbe
from ai_video_factory.domain import NarrationAudio


def _plan() -> CompositionPlan:
    return CompositionPlan(
        width=768,
        height=1280,
        fps=24,
        total_frames=48,
        shots=[
            CompositionShot(
                shot_id=1,
                uri="shot_001.mp4",
                start_frame=0,
                end_frame=48,
                duration_frames=48,
                source_duration_seconds=2.1,
                source_frame_count=51,
            )
        ],
    )


def _write_wav(path: Path, *, duration_seconds: float = 2.0) -> None:
    sample_rate = 24000
    frame_count = round(duration_seconds * sample_rate)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * frame_count)


def _video_probe(*, audio_stream_count: int = 0, frame_count: int = 48) -> MediaProbe:
    return MediaProbe(
        codec_name="h264",
        width=768,
        height=1280,
        fps=24.0,
        duration_seconds=2.0,
        frame_count=frame_count,
        audio_stream_count=audio_stream_count,
    )


def test_probe_wav_measures_canonical_audio(tmp_path: Path) -> None:
    audio = tmp_path / "narration.wav"
    _write_wav(audio)

    measured = probe_wav(audio)

    assert measured.sample_rate == 24000
    assert measured.channels == 1
    assert measured.sample_width_bytes == 2
    assert measured.frame_count == 48000
    assert measured.duration_seconds == pytest.approx(2.0)


def test_build_final_mux_command_streamcopies_video_and_encodes_aac(tmp_path: Path) -> None:
    command = build_final_mux_command(
        tmp_path / "visual_motion.mp4",
        tmp_path / "narration.wav",
        tmp_path / "final_video.mp4",
        duration_seconds=2.0,
        audio_bitrate_kbps=192,
    )

    assert command[0] == "ffmpeg"
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-b:a") + 1] == "192k"
    assert command[command.index("-t") + 1] == "2.000000"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert [command[index + 1] for index, value in enumerate(command) if value == "-map"] == [
        "0:v:0",
        "1:a:0",
    ]


def test_validate_final_mux_inputs_preserves_canonical_timeline(tmp_path: Path) -> None:
    visual = tmp_path / "visual_motion.mp4"
    visual.touch()
    audio = tmp_path / "narration.wav"
    _write_wav(audio)
    narration = NarrationAudio(uri="narration.wav", duration_seconds=2.0)

    inputs = validate_final_mux_inputs(
        _plan(),
        narration,
        visual,
        audio,
        probe_video_fn=lambda _: _video_probe(),
    )

    assert inputs.canonical_duration_seconds == pytest.approx(2.0)
    assert inputs.video.frame_count == 48
    assert inputs.narration.duration_seconds == pytest.approx(2.0)


def test_validate_final_mux_inputs_allows_metadata_rounding_within_one_frame(
    tmp_path: Path,
) -> None:
    visual = tmp_path / "visual_motion.mp4"
    visual.touch()
    audio = tmp_path / "narration.wav"
    _write_wav(audio, duration_seconds=2.02)
    narration = NarrationAudio(uri="narration.wav", duration_seconds=2.0)

    inputs = validate_final_mux_inputs(
        _plan(),
        narration,
        visual,
        audio,
        probe_video_fn=lambda _: _video_probe(),
    )

    assert inputs.narration.duration_seconds == pytest.approx(2.02)


def test_validate_final_mux_inputs_rejects_wrong_audio_or_visual(tmp_path: Path) -> None:
    visual = tmp_path / "visual_motion.mp4"
    visual.touch()
    audio = tmp_path / "narration.wav"
    _write_wav(audio)

    with pytest.raises(ValueError, match="Narration WAV duration differs"):
        validate_final_mux_inputs(
            _plan(),
            NarrationAudio(uri="narration.wav", duration_seconds=1.5),
            visual,
            audio,
            probe_video_fn=lambda _: _video_probe(),
        )

    _write_wav(audio, duration_seconds=2.1)
    with pytest.raises(ValueError, match="canonical composition"):
        validate_final_mux_inputs(
            _plan(),
            NarrationAudio(uri="narration.wav", duration_seconds=2.1),
            visual,
            audio,
            probe_video_fn=lambda _: _video_probe(),
        )

    with pytest.raises(ValueError, match="must remain silent"):
        validate_final_mux_inputs(
            _plan(),
            NarrationAudio(uri="narration.wav", duration_seconds=2.0),
            visual,
            audio,
            probe_video_fn=lambda _: _video_probe(audio_stream_count=1),
        )


def test_parse_audio_stream_payload_and_validate_final_output(tmp_path: Path) -> None:
    audio_probe = parse_audio_stream_payload(
        {
            "streams": [
                {
                    "codec_name": "aac",
                    "sample_rate": "24000",
                    "channels": 1,
                    "duration": "2.005",
                }
            ]
        }
    )
    assert audio_probe.codec_name == "aac"
    assert audio_probe.sample_rate == 24000
    assert audio_probe.channels == 1

    output = tmp_path / "final_video.mp4"
    output.touch()
    result = validate_final_mux_output(
        _plan(),
        output,
        probe_video_fn=lambda _: _video_probe(audio_stream_count=1),
        probe_audio_fn=lambda _: audio_probe,
    )

    assert result.video.frame_count == 48
    assert result.audio.codec_name == "aac"


def test_validate_final_mux_output_rejects_missing_or_non_aac_audio(tmp_path: Path) -> None:
    output = tmp_path / "final_video.mp4"
    output.touch()

    with pytest.raises(ValueError, match="exactly one audio stream"):
        validate_final_mux_output(
            _plan(),
            output,
            probe_video_fn=lambda _: _video_probe(audio_stream_count=0),
        )

    with pytest.raises(ValueError, match="must use AAC"):
        validate_final_mux_output(
            _plan(),
            output,
            probe_video_fn=lambda _: _video_probe(audio_stream_count=1),
            probe_audio_fn=lambda _: AudioStreamProbe(
                codec_name="mp3",
                sample_rate=24000,
                channels=1,
                duration_seconds=2.0,
            ),
        )
