"""Gemini 3.8 TTS adapter for the provider-neutral Phase 5 narration workflow."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import math
import wave
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .speech import GeneratedSpeech, SpeechFormat

DEFAULT_GEMINI_TTS_MODEL = "gemini-3.8-flash-tts"
DEFAULT_GEMINI_TTS_VOICE = "Algenib"
DEFAULT_GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_SAMPLE_RATE_HZ = 24_000
DEFAULT_CHANNELS = 1
MAX_STYLE_PROMPT_BYTES = 4_000

_KNOWN_PREBUILT_VOICES = frozenset({
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
})

_VERBATIM = "The text is the verbatim transcript. Do not add, omit, or rewrite words."


class GeminiTtsError(RuntimeError):
    """The Gemini response or its audio did not satisfy the TTS contract."""


def _fit_utf8(value: str, *, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def _build_style(instructions: str, speed: float) -> str:
    if not math.isfinite(speed) or not 0.25 <= speed <= 4.0:
        raise ValueError("Gemini TTS speed must be between 0.25 and 4.0")
    if speed < 1.0:
        pace = "Use a measured, slightly slower pace."
    elif speed > 1.0:
        pace = "Use a brisk pace."
    else:
        pace = "Use a natural conversational pace."

    # Reserve room for both mandatory rules even when optional instructions are oversized.
    required = f"{_VERBATIM}\n{pace}"
    available = MAX_STYLE_PROMPT_BYTES - len(required.encode("utf-8")) - 1
    optional = _fit_utf8(instructions.strip(), max_bytes=available)
    return f"{optional}\n{required}" if optional else required


def validate_gemini_voice_name(value: str) -> str:
    """Accept legacy provider IDs and exact Google Studio display names.

    Names may contain ordinary spaces, e.g. "Uncle Enzo 3". Never silently
    normalize internal whitespace: Google resolves the literal selected name.
    """
    if (
        not isinstance(value, str) or not 2 <= len(value) <= 256
        or value != value.strip() or not value[0].isalnum()
        or not value[-1].isalnum()
        or any(not (char.isalnum() or char in " -_") for char in value)
        or "  " in value
    ):
        raise ValueError("Gemini voice must be a valid ID or exact Studio display name")
    return value


def resolve_gemini_voice_id(
    value: str,
    *,
    api_key: str | None,
    api_base_url: str = DEFAULT_GEMINI_API_BASE_URL,
    timeout_seconds: float = 30.0,
) -> str:
    """Resolve a Voice Library display name to the ID accepted by Gemini.

    Existing voice IDs and offline/test calls remain unchanged.  When an API
    key is available, display names are looked up through the same REST
    ``GET /v1beta/voices`` endpoint used by the operator tooling.
    """
    selected = validate_gemini_voice_name(value)
    if (
        not api_key
        or selected.startswith(("voice_", "voicekey_"))
        or selected in _KNOWN_PREBUILT_VOICES
    ):
        return selected

    page_token: str | None = None
    for _ in range(20):
        query = "pageSize=1000"
        if page_token:
            query += "&pageToken=" + quote(page_token, safe="")
        request = Request(
            f"{api_base_url.rstrip('/')}/voices?{query}",
            headers={"x-goog-api-key": api_key.strip()},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            raise GeminiTtsError(
                f"Gemini voice lookup returned HTTP {exc.code}"
            ) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise GeminiTtsError("Gemini voice lookup failed") from exc
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GeminiTtsError("Gemini voice lookup returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GeminiTtsError("Gemini voice lookup returned an invalid response")
        voices = payload.get("voices")
        if not isinstance(voices, list):
            raise GeminiTtsError("Gemini voice lookup returned no voice list")
        for voice in voices:
            if not isinstance(voice, dict):
                continue
            identifier = voice.get("id")
            display_name = voice.get("display_name", voice.get("displayName"))
            if (
                isinstance(identifier, str)
                and isinstance(display_name, str)
                and display_name == selected
            ):
                return identifier
            if isinstance(identifier, str) and identifier == selected:
                return identifier
        page_token = payload.get("next_page_token", payload.get("nextPageToken"))
        if not isinstance(page_token, str) or not page_token:
            break

    # Keep the established built-in voice path working even if the list
    # endpoint is unavailable or omits a standard voice from its response.
    if " " not in selected:
        return selected
    raise GeminiTtsError(
        f"Gemini voice display name not found: {selected!r}; "
        "use the matching voice_... ID"
    )


def _voice_config(voice: str) -> dict[str, Any]:
    name = validate_gemini_voice_name(voice)
    # Preserve the existing ID-based wire contract for Algenib, Kore and
    # voice_... IDs. Multiword Studio names use Google's exported SDK
    # structure: VoiceConfig(prebuilt_voice_config.voice_name=...).
    if " " in name:
        return {"prebuiltVoiceConfig": {"voiceName": name}}
    return {"voice": name}


def _generation_config(voice: str) -> dict[str, Any]:
    config: dict[str, Any] = {
        "responseModalities": ["AUDIO"],
        "speechConfig": {"voiceConfig": _voice_config(voice)},
    }
    if " " not in voice:
        # Keep legacy successful ID/default requests byte-compatible.
        config["responseFormat"] = {
            "audio": {"mimeType": "AUDIO_WAV", "sampleRate": DEFAULT_SAMPLE_RATE_HZ}
        }
    # Multiword Studio voices use the exact speech_config shape exported
    # by Google AI Studio; Gemini may respond with audio/L16 raw PCM.
    return config


def _build_payload(
    *, text: str, voice: str, instructions: str, speed: float
) -> dict[str, Any]:
    return {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": text,
                        "speech_metadata": {"style": _build_style(instructions, speed)},
                    }
                ],
            }
        ],
        "generationConfig": _generation_config(voice),
    }



def _build_directed_payload(*, turns: list[dict[str, str]], voice: str) -> dict[str, Any]:
    """One Gemini 3.8 request per voice block, style per part, tags only inline."""
    if not turns:
        raise ValueError("Gemini directed TTS needs at least one speech turn")
    parts: list[dict[str, Any]] = []
    for turn in turns:
        spoken, style = turn.get("text"), turn.get("style")
        if not isinstance(spoken, str) or not spoken.strip():
            raise ValueError("Gemini directed TTS requires non-empty transcript turns")
        if (not isinstance(style, str) or not style.strip()
            or len((style.strip() + "\n" + _VERBATIM).encode("utf-8")) > 1500):
            raise ValueError("Gemini directed TTS requires a concise style on each turn")
        parts.append({
            "text": spoken,
            "speech_metadata": {"style": style.strip() + "\n" + _VERBATIM},
        })
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": _generation_config(voice),
    }

def _post_json(
    *, url: str, api_key: str, payload: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except HTTPError as exc:
        # Do not echo server-controlled error bodies; they may contain credentials.
        raise GeminiTtsError(f"Gemini TTS returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        # A failed POST may have been billed. Never retry it automatically.
        raise GeminiTtsError(
            "Gemini TTS transport failed; submission outcome may be unknown"
        ) from exc

    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeminiTtsError("Gemini TTS returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise GeminiTtsError("Gemini TTS response must be a JSON object")
    return decoded


def _decode_audio(payload: dict[str, Any]) -> bytes:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise GeminiTtsError("Gemini TTS returned no candidates")

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("inlineData"), dict):
                continue
            inline_data = part["inlineData"]
            mime = str(inline_data.get("mimeType") or "")
            mime_type = mime.split(";", 1)[0].strip().lower()
            if mime_type not in {"audio/wav", "audio/x-wav", "audio/l16"}:
                raise GeminiTtsError(
                    f"Gemini TTS returned unexpected MIME type: {mime_type or '<missing>'}"
                )
            raw_data = inline_data.get("data")
            if not isinstance(raw_data, str) or not raw_data.strip():
                raise GeminiTtsError("Gemini TTS returned empty audio data")
            try:
                audio = base64.b64decode(raw_data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise GeminiTtsError("Gemini TTS returned invalid base64 audio") from exc
            if not audio:
                raise GeminiTtsError("Gemini TTS returned empty audio bytes")
            if mime_type == "audio/l16":
                # Google Studio export can return raw 24-kHz PCM instead of a
                # WAV container. Canonicalize it before the existing WAV check.
                params = {}
                for segment in mime.split(";")[1:]:
                    key, sep, value = segment.strip().partition("=")
                    if sep:
                        params[key.strip().lower()] = value.strip()
                if params.get("rate") != str(DEFAULT_SAMPLE_RATE_HZ) or (
                    params.get("channels", "1") != "1"
                ):
                    raise GeminiTtsError("Gemini TTS returned unsupported PCM audio format")
                if len(audio) % 2:
                    raise GeminiTtsError("Gemini TTS returned incomplete PCM16 samples")
                out = io.BytesIO()
                with wave.open(out, "wb") as wav_file:
                    wav_file.setnchannels(DEFAULT_CHANNELS)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(DEFAULT_SAMPLE_RATE_HZ)
                    wav_file.writeframes(audio)
                return out.getvalue()
            return audio

    raise GeminiTtsError("Gemini TTS returned no inline audio")


def _validate_wav(data: bytes) -> dict[str, int]:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise GeminiTtsError("Gemini TTS returned invalid WAV container")
    # A standard, non-streaming Gemini WAV must not claim more bytes than received.
    if int.from_bytes(data[4:8], "little") + 8 > len(data):
        raise GeminiTtsError("Gemini TTS returned truncated WAV container")

    try:
        with wave.open(io.BytesIO(data), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            compression = wav_file.getcomptype()
            frame_count = wav_file.getnframes()
            frames = wav_file.readframes(frame_count)
    except (EOFError, OSError, ValueError, wave.Error) as exc:
        raise GeminiTtsError("Gemini TTS returned unreadable WAV audio") from exc

    if sample_rate != DEFAULT_SAMPLE_RATE_HZ:
        raise GeminiTtsError(
            f"Gemini TTS returned {sample_rate} Hz; expected {DEFAULT_SAMPLE_RATE_HZ} Hz"
        )
    if channels != DEFAULT_CHANNELS:
        raise GeminiTtsError(f"Gemini TTS returned {channels} channels; expected mono")
    if sample_width != 2 or compression != "NONE":
        raise GeminiTtsError("Gemini TTS returned audio outside PCM16 WAV contract")
    if frame_count <= 0 or not frames:
        raise GeminiTtsError("Gemini TTS returned empty WAV audio")
    if len(frames) != frame_count * channels * sample_width:
        raise GeminiTtsError("Gemini TTS returned incomplete WAV frames")
    return {"sample_rate_hz": sample_rate, "channels": channels}


class GeminiTtsSpeechProvider:
    """Direct Gemini API adapter; owns no storage, queue, or application settings."""

    def __init__(
        self,
        *,
        api_key: str,
        api_base_url: str = DEFAULT_GEMINI_API_BASE_URL,
        timeout_seconds: float = 120.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Gemini TTS requires a non-empty API key")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._api_key = api_key.strip()
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)


    async def generate_directed_speech(
        self, *, turns: list[dict[str, str]], model: str, voice: str
    ) -> GeneratedSpeech:
        """Generate one directed professional voice block without re-voicing it."""
        if not model.strip() or not voice.strip():
            raise ValueError("Gemini directed TTS requires a model and voice")
        voice = validate_gemini_voice_name(voice)
        payload = _build_directed_payload(turns=turns, voice=voice)
        return await asyncio.to_thread(self._generate_directed_sync, payload, model, voice)

    def _generate_directed_sync(
        self, payload: dict[str, Any], model: str, voice: str
    ) -> GeneratedSpeech:
        response = _post_json(
            url=f"{self._api_base_url}/models/{quote(model, safe='-._')}:generateContent",
            api_key=self._api_key,
            payload=payload,
            timeout_seconds=self._timeout_seconds,
        )
        audio = _decode_audio(response)
        info = _validate_wav(audio)
        return GeneratedSpeech(
            content=audio,
            media_type="audio/wav",
            extension="wav",
            metadata={
                "provider": "gemini", "model": model, "voice": voice,
                "sample_rate_hz": info["sample_rate_hz"],
                "channels": info["channels"],
            },
        )

    async def generate_speech(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        instructions: str,
        speed: float,
        output_format: SpeechFormat,
    ) -> GeneratedSpeech:
        if output_format != "wav":
            raise ValueError("Gemini TTS provider only supports WAV")
        if not text.strip():
            raise ValueError("Gemini TTS requires non-empty text")
        if not model.strip():
            raise ValueError("Gemini TTS requires a model")
        if not voice.strip():
            raise ValueError("Gemini TTS requires a voice")
        voice = validate_gemini_voice_name(voice)
        return await asyncio.to_thread(
            self._generate_sync,
            text=text,
            model=model.strip(),
            voice=voice.strip(),
            instructions=instructions,
            speed=speed,
        )

    def _generate_sync(
        self, *, text: str, model: str, voice: str, instructions: str, speed: float
    ) -> GeneratedSpeech:
        payload = _build_payload(
            text=text, voice=voice, instructions=instructions, speed=speed
        )
        response = _post_json(
            url=f"{self._api_base_url}/models/{quote(model, safe='-._')}:generateContent",
            api_key=self._api_key,
            payload=payload,
            timeout_seconds=self._timeout_seconds,
        )
        audio = _decode_audio(response)
        info = _validate_wav(audio)
        return GeneratedSpeech(
            content=audio,
            media_type="audio/wav",
            extension="wav",
            metadata={
                "provider": "gemini",
                "model": model,
                "voice": voice,
                "sample_rate_hz": info["sample_rate_hz"],
                "channels": info["channels"],
            },
        )
