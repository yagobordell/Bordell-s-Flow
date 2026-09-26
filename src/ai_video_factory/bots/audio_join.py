"""Sample-accurate 48 kHz PCM narration, including exactly 500 ms between blocks."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

_SAMPLE_RATE = 48000
_GAP_FRAMES = _SAMPLE_RATE // 2


class AudioJoinError(RuntimeError):
    """A block is missing, corrupt, or cannot be decoded for final narration."""


def join_audio_blocks(inputs: list[Path], destination: Path) -> dict[str, object]:
    """Decode each TTS source once, then concatenate PCM samples without timing drift.

    The resulting per-block offsets come from the exact WAV sample counts, not
    compressed-file metadata or guessed TTS durations. The same PCM samples are
    used by the final video soundtrack and the word-to-beat timeline.
    """
    if not inputs:
        raise AudioJoinError("Cannot join an empty list of audio blocks")
    for source in inputs:
        if source.is_symlink() or not source.is_file() or source.stat().st_size == 0:
            raise AudioJoinError(f"Missing or linked Fish Audio block: {source}")
    if shutil.which("ffmpeg") is None:
        raise AudioJoinError("ffmpeg is required on PATH to join Fish Audio blocks")
    if destination.parent.is_symlink() or destination.is_symlink():
        raise AudioJoinError("Refusing linked narration output directory or file")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".part.wav")
    if temporary.is_symlink():
        raise AudioJoinError("Refusing linked temporary narration output")
    temporary.unlink(missing_ok=True)
    durations: list[float] = []
    starts: list[float] = []
    ends: list[float] = []
    frame_position = 0

    try:
        with tempfile.TemporaryDirectory(
            prefix="fish-pcm-", dir=destination.parent
        ) as scratch:
            decoded: list[Path] = []
            for index, source in enumerate(inputs):
                pcm_path = Path(scratch) / f"block_{index}.wav"
                command = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-i", str(source), "-map", "0:a:0", "-vn",
                    "-c:a", "pcm_s16le", "-ar", str(_SAMPLE_RATE), "-ac", "1",
                    "-f", "wav", str(pcm_path),
                ]
                result = subprocess.run(
                    command, capture_output=True, text=True, check=False
                )
                if result.returncode != 0:
                    raise AudioJoinError(
                        f"Cannot decode Fish Audio block {index + 1}: "
                        + result.stderr[-1000:]
                    )
                decoded.append(pcm_path)

            with wave.open(str(temporary), "wb") as joined:
                joined.setnchannels(1)
                joined.setsampwidth(2)
                joined.setframerate(_SAMPLE_RATE)
                for index, pcm_path in enumerate(decoded):
                    if index:
                        joined.writeframesraw(bytes(_GAP_FRAMES * 2))
                        frame_position += _GAP_FRAMES
                    with wave.open(str(pcm_path), "rb") as block:
                        if (
                            block.getnchannels() != 1
                            or block.getframerate() != _SAMPLE_RATE
                            or block.getsampwidth() != 2
                            or block.getnframes() <= 0
                        ):
                            raise AudioJoinError(
                                f"Decoded Fish Audio block {index + 1} has invalid PCM"
                            )
                        count = block.getnframes()
                        starts.append(frame_position / _SAMPLE_RATE)
                        durations.append(count / _SAMPLE_RATE)
                        frame_position += count
                        ends.append(frame_position / _SAMPLE_RATE)
                        while frames := block.readframes(65536):
                            joined.writeframesraw(frames)

        with wave.open(str(temporary), "rb") as result:
            if (
                result.getnchannels() != 1
                or result.getframerate() != _SAMPLE_RATE
                or result.getsampwidth() != 2
                or result.getnframes() != frame_position
            ):
                raise AudioJoinError("Joined PCM sample count differs from block timeline")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    with destination.open("rb") as payload:
        digest = hashlib.file_digest(payload, "sha256").hexdigest()
    return {
        "file": destination.name,
        "sha256": digest,
        "bytes": destination.stat().st_size,
        "format": "wav",
        "duration_seconds": frame_position / _SAMPLE_RATE,
        "pause_between_blocks_seconds": 0.5,
        "sample_rate": _SAMPLE_RATE,
        "channels": 1,
        "block_start_seconds": starts,
        "block_end_seconds": ends,
        "block_durations_seconds": durations,
    }
