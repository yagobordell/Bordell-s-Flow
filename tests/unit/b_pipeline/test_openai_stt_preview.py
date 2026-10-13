"""Offline STT, exact beat offsets and a real static FFmpeg preview."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import struct
import subprocess
import wave
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from ai_video_factory.bots.audio_join import join_audio_blocks
from ai_video_factory.bots.fish_audio_workflow import (
    FishAudioWorkflowError,
    _transcribe_block,
)
from ai_video_factory.compositor.static_preview import (
    StaticPreviewError,
    _align_stt_tokens,
    _media_image_motion_filter,
    build_stt_beat_timeline,
    generate_static_preview,
)
from ai_video_factory.providers.openai_stt import OpenAISttClient, OpenAISttError
from scripts.pipeline import run_b_pipeline as runner


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wav(path: Path, frames: int) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes(struct.pack("<h", 3000) * frames)


def _fixture_assets(tmp_path: Path) -> tuple[dict, dict, dict]:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required for real PCM timing")
    first, second = tmp_path / "first.wav", tmp_path / "second.wav"
    _wav(first, 48000)
    _wav(second, 19200)
    folder = tmp_path / "audio" / "runs" / "test"
    folder.mkdir(parents=True)
    joined = join_audio_blocks([first, second], folder / "narration.wav")
    assert joined["duration_seconds"] == 1.9
    recordings = []
    for number, (source, words) in enumerate([
        (first, [
            {"word": "Hola", "start": 0.1, "end": 0.3},
            {"word": "mundo.", "start": 0.5, "end": 0.9},
        ]),
        (second, [{"word": "Adiós", "start": 0.05, "end": 0.3}]),
    ], start=1):
        block_dir = folder / "blocks" / f"block_{number}"
        block_dir.mkdir(parents=True)
        transcript = block_dir / "stt.json"
        transcript.write_text(json.dumps({
            "model": "whisper-1",
            "source_audio_sha256": _sha(source),
            "words": words,
        }), encoding="utf-8")
        recordings.append({
            "block_id": number, "sha256": _sha(source),
            "stt": {
                "file": transcript.relative_to(tmp_path).as_posix(),
                "sha256": _sha(transcript), "model": "whisper-1",
            },
        })
    audio = {
        **joined, "file": (folder / "narration.wav").relative_to(tmp_path).as_posix(),
        "blocks": recordings, "stt_complete": True, "stt_model": "whisper-1",
    }
    avatar = tmp_path / "avatar.png"
    Image.new("RGB", (1280, 720), (240, 15, 15)).save(avatar)
    media1 = tmp_path / "images" / "block_1" / "1B.png"
    media2 = tmp_path / "images" / "block_2" / "2A.png"
    media1.parent.mkdir(parents=True)
    media2.parent.mkdir(parents=True)
    illustration = Image.new("RGB", (1280, 720), (15, 15, 240))
    illustration_draw = ImageDraw.Draw(illustration)
    illustration_draw.rectangle((600, 140, 960, 410), fill=(60, 110, 245))
    illustration_draw.ellipse((800, 420, 1160, 690), fill=(50, 150, 235))
    illustration_draw.rectangle((60, 70, 180, 195), fill=(170, 35, 225))
    illustration.save(media1)
    Image.new("RGB", (1280, 720), (15, 240, 15)).save(media2)
    plan = {
        "avatar": {"file": "avatar.png", "sha256": _sha(avatar)},
        "blocks": [
            {"block_id": 1, "beats": [
                {"beat_id": "1A", "text": "Hola", "visual_type": "avatar", "description": None},
                {
                    "beat_id": "1B", "text": "mundo.", "visual_type": "media_image",
                    "description": "mundo",
                },
            ]},
            {"block_id": 2, "beats": [
                {
                    "beat_id": "2A", "text": "Adiós", "visual_type": "avatar_media",
                    "description": "despedida",
                },
            ]},
        ],
    }
    images = {"status": "completed", "items": [
        {
            "block_id": 1, "beat_id": "1B",
            "file": media1.relative_to(tmp_path).as_posix(), "sha256": _sha(media1),
        },
        {
            "block_id": 2, "beat_id": "2A",
            "file": media2.relative_to(tmp_path).as_posix(), "sha256": _sha(media2),
        },
    ]}
    return plan, audio, images


class _FakeTranscriptions:
    def __init__(self) -> None:
        self.calls = 0
        self.params = None

    def create(self, **kwargs):
        self.calls += 1
        self.params = kwargs
        return SimpleNamespace(model_dump=lambda **_unused: {
            "text": "Hola", "language": "es",
            "words": [{"word": " Hola", "start": 0.1, "end": 0.5}],
        })


def test_openai_stt_uses_word_timestamps_and_repairs_local_timing_anomalies(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "audio.wav"
    _wav(audio, 48000)
    calls = _FakeTranscriptions()
    client = OpenAISttClient(client=SimpleNamespace(
        audio=SimpleNamespace(transcriptions=calls)
    ))
    result = client.transcribe(audio)
    assert calls.calls == 1
    assert calls.params["model"] == "whisper-1"
    assert calls.params["response_format"] == "verbose_json"
    assert calls.params["timestamp_granularities"] == ["word"]
    assert result["words"] == [{"word": " Hola", "start": 0.1, "end": 0.5}]
    calls.create = lambda **_kwargs: {"words": [{"word": "x", "start": 1.0, "end": 0.5}]}
    repaired = client.transcribe(audio)
    assert repaired["words"] == [{"word": "x", "start": 1.0, "end": 1.0}]


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        ({"word": "final", "start": None, "end": 1.4}, (1.0, 1.4)),
        ({"word": "final", "start": -0.15, "end": 1.4}, (1.0, 1.4)),
        ({"word": "final", "start": float("nan"), "end": 1.4}, (1.0, 1.4)),
        ({"word": "final", "start": 1.1, "end": None}, (1.1, 1.6)),
        ({"word": "final", "start": None, "end": None}, (1.0, 1.6)),
    ],
)
def test_openai_stt_recovers_isolated_missing_word_timing(
    tmp_path: Path, broken: dict, expected: tuple[float, float],
) -> None:
    audio = tmp_path / "audio.wav"
    _wav(audio, 96000)
    fake = _FakeTranscriptions()
    fake.create = lambda **_kwargs: {"words": [
        {"word": "antes", "start": 0.5, "end": 1.0},
        broken,
        {"word": "despues", "start": 1.6, "end": 1.9},
    ]}
    client = OpenAISttClient(client=SimpleNamespace(
        audio=SimpleNamespace(transcriptions=fake),
    ))
    transcript = client.transcribe(audio)
    assert [word["word"] for word in transcript["words"]] == [
        "antes", "final", "despues",
    ]
    assert (transcript["words"][1]["start"], transcript["words"][1]["end"]) == expected
    assert transcript["timing_repairs"] == [{
        "index": 1,
        "fields": (
            ["start", "end"] if broken["start"] is None and broken["end"] is None
            else ["start"] if broken["start"] is None
            or (isinstance(broken["start"], float) and
                (broken["start"] < 0 or not math.isfinite(broken["start"])))
            else ["end"]
        ),
    }]


@pytest.mark.parametrize(
    "broken",
    [
        {"word": "x", "start": None, "end": None},
        {"word": "x", "start": -99.0, "end": None},
        {"word": "x", "start": None, "end": 12.0},
    ],
)
def test_openai_stt_refuses_unbounded_word_timing(
    tmp_path: Path, broken: dict,
) -> None:
    audio = tmp_path / "audio.wav"
    _wav(audio, 48000)
    fake = _FakeTranscriptions()
    fake.create = lambda **_kwargs: {"words": [broken]}
    client = OpenAISttClient(client=SimpleNamespace(
        audio=SimpleNamespace(transcriptions=fake),
    ))
    with pytest.raises(OpenAISttError, match="Unrecoverable"):
        client.transcribe(audio)


def test_cached_stt_repairs_are_not_billed_twice(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    _wav(audio, 96000)
    calls = _FakeTranscriptions()
    def transcript(**_kwargs):
        calls.calls += 1
        return {"words": [
            {"word": "antes", "start": 0.5, "end": 1.0},
            {"word": "final", "start": None, "end": 1.4},
        ]}
    calls.create = transcript
    client = OpenAISttClient(client=SimpleNamespace(
        audio=SimpleNamespace(transcriptions=calls),
    ))
    run = tmp_path / "audio" / "runs" / "test"
    (run / "blocks" / "block_1").mkdir(parents=True)
    item = {"block_id": 1, "file": "audio.wav", "sha256": _sha(audio)}
    first = asyncio.run(_transcribe_block(
        item, output=tmp_path, run_dir=run, stt_client=client,
    ))
    second = asyncio.run(_transcribe_block(
        item, output=tmp_path, run_dir=run, stt_client=client,
    ))
    assert first == second
    assert calls.calls == 1
    persisted = json.loads(
        (run / "blocks" / "block_1" / "stt.json").read_text(encoding="utf-8")
    )
    assert persisted["timing_repairs"] == [{"index": 1, "fields": ["start"]}]
    assert persisted["source_audio_sha256"] == item["sha256"]


def test_invalid_paid_stt_response_is_preserved_without_second_post(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "audio.wav"
    _wav(audio, 96000)
    digest = _sha(audio)
    item = {"block_id": 1, "file": "audio.wav", "sha256": digest}
    run = tmp_path / "audio" / "runs" / "test"
    folder = run / "blocks" / "block_1"
    folder.mkdir(parents=True)
    calls = _FakeTranscriptions()

    def invalid(**_kwargs):
        calls.calls += 1
        return {"words": [{"word": None, "start": 0.1, "end": 0.3}]}

    calls.create = invalid
    client = OpenAISttClient(client=SimpleNamespace(
        audio=SimpleNamespace(transcriptions=calls),
    ))
    for _ in range(2):
        with pytest.raises(OpenAISttError, match="non-text word"):
            asyncio.run(_transcribe_block(
                item, output=tmp_path, run_dir=run, stt_client=client,
            ))
    assert calls.calls == 1
    raw = json.loads((folder / "stt_response.json").read_text(encoding="utf-8"))
    assert raw["source_audio_sha256"] == digest
    assert raw["response"]["words"][0]["word"] is None
    request = json.loads((folder / "stt_request.json").read_text(encoding="utf-8"))
    assert request["status"] == "response_invalid"
    assert _sha(audio) == digest


@pytest.mark.parametrize("blank", ["  ", "\t", "...", "—", "!?"])
def test_whisper_nonlexical_word_is_skipped_and_reported(
    tmp_path: Path, blank: str,
) -> None:
    audio = tmp_path / "audio.wav"
    _wav(audio, 48000)
    calls = _FakeTranscriptions()
    calls.create = lambda **_kwargs: {"words": [
        {"word": "Hola", "start": 0.1, "end": 0.3},
        {"word": blank, "start": None, "end": None},
        {"word": "mundo", "start": 0.4, "end": 0.7},
    ]}
    client = OpenAISttClient(client=SimpleNamespace(
        audio=SimpleNamespace(transcriptions=calls),
    ))
    transcript = client.transcribe(audio)
    assert [entry["word"] for entry in transcript["words"]] == ["Hola", "mundo"]
    assert transcript["skipped_nonlexical_indices"] == [1]
    assert [entry["start"] for entry in transcript["words"]] == [0.1, 0.4]


def test_whisper_nonlexical_at_original_error_index_348(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    _wav(audio, 48000)
    calls = _FakeTranscriptions()
    calls.create = lambda **_kwargs: {"words": [
        *({"word": "Hola", "start": i / 1000, "end": (i + 1) / 1000}
          for i in range(348)),
        {"word": " ", "start": None, "end": None},
        {"word": "mundo", "start": 0.35, "end": 0.37},
    ]}
    client = OpenAISttClient(client=SimpleNamespace(
        audio=SimpleNamespace(transcriptions=calls),
    ))
    transcript = client.transcribe(audio)
    assert len(transcript["words"]) == 349
    assert transcript["words"][-1]["word"] == "mundo"
    assert transcript["skipped_nonlexical_indices"] == [348]


@pytest.mark.parametrize("invalid", [None, 6, {"text": "mundo"}])
def test_whisper_nontext_word_is_rejected_not_silently_dropped(
    tmp_path: Path, invalid: object,
) -> None:
    audio = tmp_path / "audio.wav"
    _wav(audio, 48000)
    calls = _FakeTranscriptions()
    calls.create = lambda **_kwargs: {"words": [
        {"word": "Hola", "start": 0.1, "end": 0.3},
        {"word": invalid, "start": 0.4, "end": 0.7},
    ]}
    client = OpenAISttClient(client=SimpleNamespace(
        audio=SimpleNamespace(transcriptions=calls),
    ))
    with pytest.raises(OpenAISttError, match="non-text word"):
        client.transcribe(audio)


def test_stt_raw_cache_rejects_changed_source_hash(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    _wav(audio, 48000)
    calls = _FakeTranscriptions()
    client = OpenAISttClient(client=SimpleNamespace(
        audio=SimpleNamespace(transcriptions=calls),
    ))
    checkpoint = tmp_path / "stt_response.json"
    client.transcribe_durable(
        audio, response_path=checkpoint, source_audio_sha256="first",
    )
    with pytest.raises(OpenAISttError, match="differs from source"):
        client.transcribe_durable(
            audio, response_path=checkpoint, source_audio_sha256="second",
        )
    assert calls.calls == 1


def test_completed_tts_block_stt_is_cached_without_second_post(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    _wav(audio, 48000)
    item = {"block_id": 1, "file": "audio.wav", "sha256": _sha(audio)}
    calls = _FakeTranscriptions()
    client = OpenAISttClient(client=SimpleNamespace(
        audio=SimpleNamespace(transcriptions=calls)
    ))
    run = tmp_path / "audio" / "runs" / "test"
    (run / "blocks" / "block_1").mkdir(parents=True)
    first = asyncio.run(_transcribe_block(
        item, output=tmp_path, run_dir=run, stt_client=client
    ))
    again = asyncio.run(_transcribe_block(
        item, output=tmp_path, run_dir=run, stt_client=client
    ))
    assert first == again
    assert calls.calls == 1
    assert first["stt"]["word_count"] == 1
    (run / "blocks" / "block_1" / "stt.json").unlink()
    with pytest.raises(FishAudioWorkflowError, match="never repeat"):
        asyncio.run(_transcribe_block(
            item, output=tmp_path, run_dir=run, stt_client=client
        ))
    request = run / "blocks" / "block_1" / "stt_request.json"
    state = json.loads(request.read_text(encoding="utf-8"))
    state["status"] = "request_started_unknown"
    request.write_text(json.dumps(state), encoding="utf-8")
    # A complete raw provider response is safe to replay even if the task
    # manifest was interrupted after the paid call.
    restored = asyncio.run(_transcribe_block(
        item, output=tmp_path, run_dir=run, stt_client=client,
    ))
    assert restored["stt"]["word_count"] == 1
    assert calls.calls == 1


def test_timeline_uses_exact_sample_offsets_and_holds_image_during_gap(
    tmp_path: Path,
) -> None:
    plan, audio, _images = _fixture_assets(tmp_path)
    result = build_stt_beat_timeline(tmp_path, plan, audio)
    beats = result["beats"]
    assert [beat["beat_id"] for beat in beats] == ["1A", "1B", "2A"]
    assert [beat["start_seconds"] for beat in beats] == [0.0, 0.5, 1.5]
    assert [beat["end_seconds"] for beat in beats] == [0.5, 1.5, 1.9]
    assert beats[1]["last_word_end_seconds"] == 0.9
    assert audio["block_durations_seconds"] == [1.0, 0.4]
    assert audio["block_start_seconds"] == [0.0, 1.5]


def test_timeline_fails_closed_when_stt_changes_a_word(tmp_path: Path) -> None:
    plan, audio, images = _fixture_assets(tmp_path)
    path = tmp_path / audio["blocks"][0]["stt"]["file"]
    transcript = json.loads(path.read_text(encoding="utf-8"))
    transcript["words"][1]["word"] = "casa"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    audio["blocks"][0]["stt"]["sha256"] = _sha(path)
    with pytest.raises(StaticPreviewError, match="do not match"):
        generate_static_preview(tmp_path, plan, audio, images)
    assert not (tmp_path / "preview" / "static_preview.mp4").exists()


def test_timeline_ignores_bracketed_stage_direction_but_not_plain_words(
    tmp_path: Path,
) -> None:
    plan, audio, _images = _fixture_assets(tmp_path)
    plan["blocks"][0]["beats"][0]["text"] = "[brief pause]\nHola"

    timeline = build_stt_beat_timeline(tmp_path, plan, audio)
    assert timeline["beats"][0]["word_count"] == 1

    plan["blocks"][0]["beats"][0]["text"] = "brief pause Hola"
    with pytest.raises(StaticPreviewError, match="do not match"):
        build_stt_beat_timeline(tmp_path, plan, audio)


def test_stt_accepts_spoken_currency_as_collapsed_numeric_token() -> None:
    mapping = _align_stt_tokens(
        ["forty", "seven", "dollars"], ["47"], block_id=3,
    )
    assert mapping == [0, 0, 0]


def test_real_ffmpeg_preview_has_audio_cuts_and_avatar_media_50_50(
    tmp_path: Path,
) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is required for MP4 validation")
    plan, audio, images = _fixture_assets(tmp_path)
    artifact = generate_static_preview(tmp_path, plan, audio, images)
    assert artifact["status"] == "completed"
    assert artifact["beat_count"] == 3
    assert artifact["avatar_media_layout"] == "side_by_side_50_50"
    assert artifact["media_image_motion"]["animated_beats"] == 1
    assert abs(artifact["rendered_duration_seconds"] - 1.9) < 0.15
    assert _sha(tmp_path / artifact["file"]) == artifact["sha256"]
    frame = tmp_path / "preview" / "frames" / "0002_2A.png"
    with Image.open(frame) as still:
        left, right = still.getpixel((200, 360)), still.getpixel((1080, 360))
    assert left[0] > left[1] and left[0] > left[2]
    assert right[1] > right[0] and right[1] > right[2]

    def rendered_pixel(seconds: float, x: int, y: int) -> tuple[int, int, int]:
        decoded = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(tmp_path / artifact["file"]), "-ss", str(seconds),
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
            ],
            capture_output=True, check=True,
        )
        with Image.open(BytesIO(decoded.stdout)) as image:
            return image.convert("RGB").getpixel((x, y))

    intro = rendered_pixel(0.25, 200, 360)
    illustration = rendered_pixel(1.0, 200, 360)
    split_avatar = rendered_pixel(1.7, 200, 360)
    split_media = rendered_pixel(1.7, 1080, 360)
    assert intro[0] > intro[1] and intro[0] > intro[2]
    assert illustration[2] > illustration[0] and illustration[2] > illustration[1]
    assert split_avatar[0] > split_avatar[1] and split_avatar[0] > split_avatar[2]
    assert split_media[1] > split_media[0] and split_media[1] > split_media[2]

    saved = json.loads((tmp_path / artifact["timeline_file"]).read_text(encoding="utf-8"))
    assert saved["beats"][2]["start_seconds"] == 1.5
    assert saved["beats"][1]["motion"] == {
        "version": "media-image-subpixel-affine-v3",
        "mode": "ken_burns",
        "direction": "out",
        "origin": "bottom_left",
        "zoom_start": 1.02,
        "zoom_end": 1.0,
    }
    assert all("motion" not in saved["beats"][index] for index in (0, 2))



def test_media_image_motion_matches_media_pipeline_zoom_model() -> None:
    beats = [
        {
            "beat_id": f"1{chr(ord('B') + index)}",
            "visual_type": "media_image",
            "start_frame": index * 30,
            "end_frame": (index + 1) * 30,
        }
        for index in range(10)
    ]
    still = {
        "beat_id": "2A", "visual_type": "media_video",
        "start_frame": 300, "end_frame": 330,
    }
    timeline = {"beats": [*beats, still]}
    video_filter = _media_image_motion_filter(timeline)

    assert video_filter.startswith("fps=30,scale=1280:720:flags=lanczos,format=yuv444p,")
    assert "zoompan=" not in video_filter
    assert ":sense=source:interpolation=linear:eval=frame" in video_filter
    assert "setsar=1" in video_filter
    assert video_filter.endswith("format=yuv420p")
    assert [beat["motion"]["direction"] for beat in beats] == [
        "out", "in", "out", "in", "out", "in", "out", "in", "out", "in",
    ]
    for index, beat in enumerate(beats):
        expected_start, expected_end = (
            (1.02, 1.0) if index % 2 == 0 else (1.0, 1.02)
        )
        assert beat["motion"] == {
            "version": "media-image-subpixel-affine-v3",
            "mode": "ken_burns",
            "direction": "out" if index % 2 == 0 else "in",
            "origin": "bottom_left",
            "zoom_start": expected_start,
            "zoom_end": expected_end,
        }
    assert "motion" not in still

    second = {"beats": [{k: v for k, v in beat.items() if k != "motion"}
                        for beat in [*beats, still]]}
    assert _media_image_motion_filter(second) == video_filter


def test_media_image_less_than_two_frames_cannot_be_published_static() -> None:
    timeline = {"beats": [{
        "beat_id": "1B", "visual_type": "media_image",
        "start_frame": 0, "end_frame": 1,
    }]}
    with pytest.raises(StaticPreviewError, match="at least two"):
        _media_image_motion_filter(timeline)
    assert _media_image_motion_filter({"beats": [{
        "beat_id": "1C", "visual_type": "media_video",
        "start_frame": 0, "end_frame": 1,
    }]}) == "fps=30,format=yuv420p"


def test_real_media_image_preview_frames_change_during_one_beat(
    tmp_path: Path,
) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is required for MP4 validation")
    plan, audio, images = _fixture_assets(tmp_path)
    artifact = generate_static_preview(tmp_path, plan, audio, images)

    def decoded_frame(seconds: float) -> Image.Image:
        output = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(tmp_path / artifact["file"]), "-ss", str(seconds),
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
            ],
            capture_output=True, check=True,
        )
        with Image.open(BytesIO(output.stdout)) as decoded:
            return decoded.convert("RGB")

    early = decoded_frame(0.65)
    late = decoded_frame(1.25)
    region = (570, 90, 1170, 690)
    change = ImageChops.difference(early.crop(region), late.crop(region))
    assert change.getbbox() is not None
    assert max(ImageStat.Stat(change).mean) > 0.5
    # Motion cannot shift the sample-accurate STT-aligned scene boundaries.
    saved = json.loads((tmp_path / artifact["timeline_file"]).read_text(encoding="utf-8"))
    assert saved["beats"][1]["start_seconds"] == 0.5
    assert saved["beats"][1]["end_seconds"] == 1.5
    assert abs(artifact["rendered_duration_seconds"] - 1.9) < 0.15



def test_real_bottom_left_ken_burns_alternates_second_media_image(
    tmp_path: Path,
) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is required for MP4 validation")
    plan, audio, images = _fixture_assets(tmp_path)
    plan["blocks"][1]["beats"][0]["visual_type"] = "media_image"
    source = tmp_path / images["items"][1]["file"]
    patterned = Image.new("RGB", (1280, 720), (15, 240, 15))
    draw = ImageDraw.Draw(patterned)
    draw.rectangle((780, 130, 1050, 380), fill=(215, 15, 15))
    draw.ellipse((250, 470, 580, 660), fill=(20, 60, 220))
    patterned.save(source)
    images["items"][1]["sha256"] = _sha(source)

    artifact = generate_static_preview(tmp_path, plan, audio, images)
    timeline = json.loads(
        (tmp_path / artifact["timeline_file"]).read_text(encoding="utf-8")
    )
    assert artifact["media_image_motion"]["animated_beats"] == 2
    assert timeline["beats"][2]["motion"] == {
        "version": "media-image-subpixel-affine-v3",
        "mode": "ken_burns",
        "direction": "in",
        "origin": "bottom_left",
        "zoom_start": 1.0,
        "zoom_end": 1.008,
    }

    def frame_at(seconds: float) -> Image.Image:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(tmp_path / artifact["file"]), "-ss", str(seconds),
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
            ],
            capture_output=True, check=True,
        )
        with Image.open(BytesIO(result.stdout)) as frame:
            return frame.convert("RGB")

    early = frame_at(1.55)
    late = frame_at(1.82)
    region = (740, 90, 1180, 430)
    movement = ImageChops.difference(early.crop(region), late.crop(region))
    assert movement.getbbox() is not None
    assert max(ImageStat.Stat(movement).mean) > 0.5
    assert _sha(source) == images["items"][1]["sha256"]
    assert abs(artifact["rendered_duration_seconds"] - 1.9) < 0.15



def test_images_only_auto_renders_from_existing_stt_and_saved_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "input" / "roma.txt"
    script.parent.mkdir()
    script.write_text("Hola mundo. Adiós", encoding="utf-8")
    output = tmp_path / "output" / "roma"
    output.mkdir(parents=True)
    plan, audio, images = _fixture_assets(output)
    images["model_id"] = "gpt-image-2.5-flare"
    (output / "B2").mkdir()
    (output / "B2" / "merged_output.json").write_text("{}", encoding="utf-8")
    (output / "visual_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (output / "run_report.json").write_text(json.dumps({
        "run": {
            "script_sha256": _sha(script), "status": "failed",
            "audio_generation": {"status": "completed", **audio},
        },
        "api_costs": {"run_status": "failed"},
        "timings": {"status": "failed"},
    }), encoding="utf-8")
    # Images-only removes untracked old PNGs before requesting fresh images.
    # Simulate the image provider writing new artifacts with matching hashes.
    original_pngs = {
        item["file"]: (output / item["file"]).read_bytes()
        for item in images["items"]
    }
    generated = []

    async def fake_images(
        destination: Path, visual_plan: dict, *,
        allow_ambiguous_upscale_resume: bool = False,
    ) -> dict:
        assert allow_ambiguous_upscale_resume
        assert all(not (destination / path).exists() for path in original_pngs)
        for relative, data in original_pngs.items():
            image_path = destination / relative
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(data)
        generated.append(destination)
        return images

    monkeypatch.setattr(runner, "_generate_images", fake_images)

    async def forbid_ltx(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("Static-preview-only test must not dispatch LTX jobs")

    monkeypatch.setattr(runner, "_video_if_ready", forbid_ltx)
    asyncio.run(runner._run_images_only(script, tmp_path / "output", no_video=True))
    assert generated == [output]
    report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    assert report["run"]["status"] == "completed"
    preview = report["run"]["video_preview"]
    assert preview["status"] == "completed"
    assert (output / preview["file"]).is_file()
    assert report["run"]["video_generation"] == {"status": "skipped"}
