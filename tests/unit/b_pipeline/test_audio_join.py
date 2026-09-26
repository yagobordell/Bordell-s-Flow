"""Offline contract for ordered 500 ms PCM joins of separate Fish voice tracks."""

import hashlib
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from ai_video_factory.bots.audio_join import AudioJoinError, join_audio_blocks


def _write_tone(path: Path, frames: int, frequency: int) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * frequency * i / 48000)))
            for i in range(frames)
        ))


def test_join_preserves_block_order_with_exact_half_second_of_silence(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    first, second = tmp_path / "first.wav", tmp_path / "second.wav"
    _write_tone(first, 12000, 440)
    _write_tone(second, 16800, 660)
    output = tmp_path / "narration.wav"
    result = join_audio_blocks([first, second], output)
    assert result["pause_between_blocks_seconds"] == 0.5
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["format"] == "wav"
    with wave.open(str(output), "rb") as audio:
        assert audio.getframerate() == 48000
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getnframes() == 12000 + 24000 + 16800
        frames = audio.readframes(audio.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    assert any(value != 0 for value in samples[:12000])
    assert all(value == 0 for value in samples[12000:36000])
    assert any(value != 0 for value in samples[36000:])


def test_join_rejects_missing_or_linked_block(tmp_path: Path) -> None:
    with pytest.raises(AudioJoinError, match="Missing"):
        join_audio_blocks([tmp_path / "missing.wav"], tmp_path / "final.wav")
