"""Deterministic lossless WAV join with exactly 500 ms between TTS blocks."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import wave
from pathlib import Path


class AudioJoinError(RuntimeError):
    """A block is missing, corrupt, or cannot be decoded for final narration."""


def join_audio_blocks(inputs: list[Path], destination: Path) -> dict[str, object]:
    """Decode mixed TTS formats once; join in block order with PCM silence.

    ffmpeg is an existing project prerequisite. Every input is decoded to
    48 kHz mono PCM; the output remains PCM WAV so the pause is not altered
    by MP3/AAC encoder padding. Never write a successful final artifact until
    the entire output has been verified.
    """
    if not inputs:
        raise AudioJoinError("Cannot join an empty list of audio blocks")
    if shutil.which("ffmpeg") is None:
        raise AudioJoinError("ffmpeg is required on PATH to join Fish Audio blocks")
    for source in inputs:
        if source.is_symlink() or not source.is_file() or source.stat().st_size == 0:
            raise AudioJoinError(f"Missing or linked Fish Audio block: {source}")

    if destination.parent.is_symlink() or destination.is_symlink():
        raise AudioJoinError("Refusing linked narration output directory or file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".part.wav")
    if temporary.is_symlink():
        raise AudioJoinError("Refusing linked temporary narration output")
    if temporary.exists():
        temporary.unlink()

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for source in inputs:
        command.extend(["-i", str(source)])

    filters: list[str] = []
    sequence: list[str] = []
    for index in range(len(inputs)):
        filters.append(
            f"[{index}:a:0]aresample=48000,"
            f"aformat=sample_fmts=s16:channel_layouts=mono,"
            f"asetpts=PTS-STARTPTS[block{index}]"
        )
        sequence.append(f"[block{index}]")
        if index < len(inputs) - 1:
            filters.append(f"anullsrc=r=48000:cl=mono:d=0.5[gap{index}]")
            sequence.append(f"[gap{index}]")
    filters.append(
        "".join(sequence) + f"concat=n={len(sequence)}:v=0:a=1[narration]"
    )
    command.extend(
        [
            "-filter_complex", ";".join(filters),
            "-map", "[narration]",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1",
            "-f", "wav", str(temporary),
        ]
    )
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise AudioJoinError(
                "ffmpeg could not assemble Fish Audio blocks: "
                + completed.stderr[-1200:]
            )
        with wave.open(str(temporary), "rb") as joined:
            if (
                joined.getnchannels() != 1
                or joined.getframerate() != 48000
                or joined.getsampwidth() != 2
                or joined.getnframes() == 0
            ):
                raise AudioJoinError("Joined narration is not nonempty 48 kHz mono PCM16")
            duration = joined.getnframes() / joined.getframerate()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    payload = destination.read_bytes()
    return {
        "file": destination.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "format": "wav",
        "duration_seconds": duration,
        "pause_between_blocks_seconds": 0.5,
        "sample_rate": 48000,
        "channels": 1,
    }
