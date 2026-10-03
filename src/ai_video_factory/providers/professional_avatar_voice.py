"""Separate persistent Gemini voice selection for professional avatars.

Never reuse Casero's Fish/MiniMax/ElevenLabs voice config.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from ai_video_factory.providers.gemini_tts import (
    DEFAULT_GEMINI_TTS_VOICE,
    resolve_gemini_voice_id,
    validate_gemini_voice_name,
)


@dataclass(frozen=True)
class ProfessionalAvatarVoice:
    voice_id: str
    provider: str = "gemini"
    audio_filename: None = None

    def snapshot(self) -> dict[str, str]:
        return {"mode": "voice_id", "status": "ready",
                "provider": self.provider, "voice_id": self.voice_id}


def ensure_professional_avatar_voice(
    *, avatar_file: Path, input_voice_id: str | None = None,
    use_default: bool = False, interactive: bool | None = None,
    allow_replace: bool = False, gemini_api_key: str | None = None,
) -> ProfessionalAvatarVoice:
    """Prompt at first use only; non-interactive CI can use the prebuilt default."""
    if not avatar_file.is_file() or avatar_file.is_symlink():
        raise ValueError("Professional avatar must be an existing regular file")
    folder = avatar_file.parent / "meta"
    path = folder / "gemini_voice.json"
    if folder.is_symlink() or path.is_symlink():
        raise ValueError("Professional voice config must not use symlinks")
    if use_default and input_voice_id is not None:
        raise ValueError("Choose --default-voice or --voice-id, not both")
    if allow_replace and (input_voice_id is None or use_default):
        raise ValueError("Replacing a professional voice requires an explicit --voice-id")
    if interactive is None:
        interactive = sys.stdin.isatty()
    current = None
    if path.is_file():
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict) or current.get("provider") != "gemini":
            raise ValueError("Saved professional Gemini voice config is malformed")
    selected = input_voice_id.strip() if input_voice_id is not None else None
    if input_voice_id is not None and not selected:
        raise ValueError("Gemini voice ID cannot be empty")
    if current is None and selected is None:
        if use_default or not interactive:
            selected = DEFAULT_GEMINI_TTS_VOICE
        else:
            choice = input(
                f"Voz de Gemini profesional (Enter = {DEFAULT_GEMINI_TTS_VOICE}): "
            ).strip()
            selected = choice or DEFAULT_GEMINI_TTS_VOICE
    if current is not None:
        saved = resolve_gemini_voice_id(
            validate_gemini_voice_name(current.get("voice_id")),
            api_key=gemini_api_key,
        )
        if selected is not None:
            selected = resolve_gemini_voice_id(selected, api_key=gemini_api_key)
        if selected is not None and selected != saved and not allow_replace:
            raise ValueError("This avatar already has a different professional Gemini voice")
        selected = selected if allow_replace else saved
    selected = resolve_gemini_voice_id(selected, api_key=gemini_api_key)
    if current is None or current["voice_id"] != selected:
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / "gemini_voice.json.tmp"
        if temporary.exists() or temporary.is_symlink():
            raise ValueError("Unreconciled professional Gemini voice config temp file")
        temporary.write_text(json.dumps({"provider": "gemini", "voice_id": selected},
                                        indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    return ProfessionalAvatarVoice(voice_id=selected)
