"""Offline STT, exact beat offsets and a real static FFmpeg preview."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.bots.audio_join import join_audio_blocks
from ai_video_factory.bots.fish_audio_workflow import (
    FishAudioWorkflowError,
    _transcribe_block,
)
from ai_video_factory.compositor.static_preview import (
    StaticPreviewError,
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
    Image.new("RGB", (1280, 720), (15, 15, 240)).save(media1)
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


def test_openai_stt_uses_word_timestamps_and_rejects_malformed_data(tmp_path: Path) -> None:
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
    with pytest.raises(OpenAISttError, match="Invalid"):
        client.transcribe(audio)


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
    with pytest.raises(FishAudioWorkflowError, match="reconcile"):
        asyncio.run(_transcribe_block(
            item, output=tmp_path, run_dir=run, stt_client=client
        ))
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
    assert abs(artifact["rendered_duration_seconds"] - 1.9) < 0.15
    assert _sha(tmp_path / artifact["file"]) == artifact["sha256"]
    frame = tmp_path / "preview" / "frames" / "0002_2A.png"
    with Image.open(frame) as still:
        left, right = still.getpixel((200, 360)), still.getpixel((1080, 360))
    assert left[0] > left[1] and left[0] > left[2]
    assert right[1] > right[0] and right[1] > right[2]
    saved = json.loads((tmp_path / artifact["timeline_file"]).read_text(encoding="utf-8"))
    assert saved["beats"][2]["start_seconds"] == 1.5


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
    generated = []

    async def fake_images(destination: Path, visual_plan: dict) -> dict:
        generated.append(destination)
        return images

    monkeypatch.setattr(runner, "_generate_images", fake_images)
    asyncio.run(runner._run_images_only(script, tmp_path / "output"))
    assert generated == [output]
    report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    assert report["run"]["status"] == "completed"
    preview = report["run"]["video_preview"]
    assert preview["status"] == "completed"
    assert (output / preview["file"]).is_file()
