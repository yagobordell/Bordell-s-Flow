import pytest

from ai_video_factory.compositor.media import parse_ffprobe_payload


def test_parses_expected_phase8_video_stream() -> None:
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 768,
                "height": 1280,
                "avg_frame_rate": "24/1",
                "nb_frames": "89",
                "duration": "3.708333",
            }
        ],
        "format": {"duration": "3.708333"},
    }

    media = parse_ffprobe_payload(payload, source="shot_001.mp4")

    assert media.codec_name == "h264"
    assert media.width == 768
    assert media.height == 1280
    assert media.fps == 24.0
    assert media.frame_count == 89
    assert media.duration_seconds == pytest.approx(3.708333)
    assert media.audio_stream_count == 0


def test_counts_audio_streams_for_upstream_validation() -> None:
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 768,
                "height": 1280,
                "avg_frame_rate": "24/1",
                "duration": "1.0",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "1.0"},
    }

    media = parse_ffprobe_payload(payload)

    assert media.audio_stream_count == 1
    assert media.frame_count is None


def test_rejects_multiple_video_streams() -> None:
    stream = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": 768,
        "height": 1280,
        "avg_frame_rate": "24/1",
        "duration": "1.0",
    }
    payload = {"streams": [stream, stream], "format": {"duration": "1.0"}}

    with pytest.raises(ValueError, match="exactly one video stream"):
        parse_ffprobe_payload(payload)
