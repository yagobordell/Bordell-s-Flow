"""OpenSpeaker asynchronous speech generation for a Fish Audio voice-source ID.

OpenSpeaker labels voice sources; this client does not claim its synthesis bridge
is the upstream Fish S2 model. Never automatically retry an ambiguous paid POST.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

_API_BASE = "https://api.openspeaker.ai"
_RETRYABLE = {429, 502, 503, 504}
_MAX_AUDIO_BYTES = 200 * 1024 * 1024


class AI33SpeechError(RuntimeError):
    """A speech task failed or its result needs manual recovery."""


class AI33SpeechTimeout(AI33SpeechError):
    """A known task is still pending after the local polling window."""


def task_id_of(response: dict[str, Any]) -> str | None:
    nested = response.get("data")
    for obj in (response, nested):
        if isinstance(obj, dict):
            for key in ("task_id", "taskId", "id"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def audio_url_of(response: dict[str, Any]) -> str | None:
    """Accept documented/common audio result fields, not unrelated image/preview URLs."""
    audio_fields = ("audio_url", "audioUrl", "audio_file_url", "audioFileUrl")
    containers = ("metadata", "data", "result", "output", "audio", "audio_result")
    queue: list[tuple[object, bool]] = [(response, False)]
    visited = 0
    while queue and visited < 28:
        obj, audio_context = queue.pop(0)
        visited += 1
        if isinstance(obj, list):
            queue.extend((entry, audio_context) for entry in obj[:5])
            continue
        if not isinstance(obj, dict):
            continue
        for key in audio_fields:
            value = obj.get(key)
            if isinstance(value, str) and value.startswith("https://"):
                return value
        if audio_context:
            for key in ("url", "download_url", "downloadUrl"):
                value = obj.get(key)
                if isinstance(value, str) and value.startswith("https://"):
                    return value
        for key in containers + ("audios", "results", "files", "result_audios"):
            if key in obj:
                queue.append((obj[key], audio_context or "audio" in key))
    return None


def _audio_extension(data: bytes) -> str:
    if data.startswith(b"ID3") or (
        len(data) > 1 and data[0] == 255 and (data[1] & 0xE0) == 0xE0
    ):
        return "mp3"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "m4a"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    raise AI33SpeechError("OpenSpeaker response is not a recognized audio file")


class AI33SpeechClient:
    def __init__(self, api_key: str, *, base_url: str = _API_BASE) -> None:
        if not api_key.strip():
            raise ValueError("AI33_API_KEY is required for Fish voice synthesis")
        if base_url != _API_BASE:
            raise ValueError("OpenSpeaker TTS must use its verified HTTPS API base")
        self._api_key = api_key
        self._base_url = base_url

    def request_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = (
            None if payload is None
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        request = Request(
            self._base_url + path,
            data=data,
            headers={
                "xi-api-key": self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        with urlopen(request, timeout=90) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise AI33SpeechError("OpenSpeaker TTS returned a non-object JSON result")
        if result.get("success") is False:
            code = result.get("code", "unknown")
            raise AI33SpeechError(f"OpenSpeaker TTS rejected {method} {path}: {code}")
        return result

    def create(self, *, text: str, voice_id: str, speed: float = 1.0) -> dict[str, Any]:
        if not text.strip() or not voice_id.startswith("fishaudio_"):
            raise ValueError("Fish TTS needs nonempty text and a Fish voice ID")
        if not 0.5 <= speed <= 1.5:
            raise ValueError("OpenSpeaker TTS speed must be between 0.5 and 1.5")
        return self.request_json(
            "POST",
            "/v3/text-to-speech",
            {"text": text, "voice_id": voice_id, "speed": speed},
        )

    def poll(
        self, task_id: str, *, timeout_seconds: int = 3600, interval_seconds: float = 8.0
    ) -> dict[str, Any]:
        if timeout_seconds < 1 or interval_seconds <= 0:
            raise ValueError("TTS polling limits must be positive")
        deadline = time.monotonic() + timeout_seconds
        attempts = 0
        path = f"/v1/task/{quote(task_id, safe='')}"
        while time.monotonic() < deadline:
            try:
                result = self.request_json("GET", path)
            except HTTPError as exc:
                if exc.code not in _RETRYABLE:
                    raise AI33SpeechError(
                        f"OpenSpeaker TTS task {task_id} failed polling: HTTP {exc.code}"
                    ) from exc
                attempts += 1
                time.sleep(min(interval_seconds * 2 ** min(attempts, 3), 30))
                continue
            except (URLError, TimeoutError):
                attempts += 1
                time.sleep(min(interval_seconds * 2 ** min(attempts, 3), 30))
                continue
            if result.get("code") == "server_busy":
                time.sleep(interval_seconds)
                continue
            attempts = 0
            status = str(result.get("status", "")).lower()
            if status in {"done", "completed", "success"}:
                return result
            if status in {"failed", "error", "cancelled", "canceled"}:
                raise AI33SpeechError(
                    f"OpenSpeaker TTS task {task_id} ended with status {status}"
                )
            time.sleep(interval_seconds)
        raise AI33SpeechTimeout(
            f"OpenSpeaker TTS task {task_id} is still pending; "
            "use --resume-audio to poll the saved task without paying again."
        )

    def download_audio(self, url: str, directory: Path) -> dict[str, Any]:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise AI33SpeechError("OpenSpeaker returned an unsafe audio download URL")
        request = Request(url, headers={"Accept": "audio/*,*/*"}, method="GET")
        with urlopen(request, timeout=180) as response:
            data = response.read(_MAX_AUDIO_BYTES + 1)
        if len(data) > _MAX_AUDIO_BYTES:
            raise AI33SpeechError("OpenSpeaker TTS result exceeds 200 MiB")
        extension = _audio_extension(data)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"audio.{extension}"
        temporary = directory / f"audio.{extension}.part"
        if directory.is_symlink() or target.is_symlink() or temporary.is_symlink():
            raise AI33SpeechError("Refusing to write linked audio output")
        try:
            temporary.write_bytes(data)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "file": target.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "format": extension,
        }
