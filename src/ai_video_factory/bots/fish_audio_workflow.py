"""Independent Fish director + OpenSpeaker TTS run, safe to run beside B1.1."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_video_factory.providers.ai33_speech import (
    AI33SpeechClient,
    AI33SpeechError,
    audio_url_of,
    task_id_of,
)
from ai_video_factory.providers.base import StructuredTextProvider

from .fish_audio import FishAudioScriptError, fish_prompt_bytes, run_fish_director

_DEFAULT_VOICE_ID = "fishaudio_f8dfe9c83081432386f143e2fe9767ef"


class FishAudioWorkflowError(RuntimeError):
    """Speech state requires recovery or has a changed source script."""


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink() or path.parent.is_symlink():
        raise FishAudioWorkflowError(f"Refusing linked Fish Audio output path: {path}")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FishAudioWorkflowError(f"Missing or linked Fish Audio state: {path}")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise FishAudioWorkflowError(f"Invalid Fish Audio state: {path}")
    return result


def unfinished_audio_runs(output: Path) -> list[Path]:
    """Prevent deleting or duplicating paid TTS tasks with unknown outcomes."""
    folder = output / "audio" / "runs"
    if folder.is_symlink():
        raise FishAudioWorkflowError("Linked Fish Audio runs directory")
    pending = []
    for state_path in sorted(folder.glob("*/task.json")):
        state = _read_json(state_path)
        if state.get("status") not in {"completed", "director_invalid"}:
            pending.append(state_path)
    return pending


def _audio_result(
    output: Path, directory: Path, state: dict[str, Any]
) -> dict[str, Any]:
    metadata = state.get("audio")
    if not isinstance(metadata, dict):
        raise FishAudioWorkflowError("Completed Fish Audio run has no audio metadata")
    audio_file = directory / metadata["file"]
    if audio_file.is_symlink() or not audio_file.is_file():
        raise FishAudioWorkflowError("Completed Fish Audio file is missing or linked")
    sha = hashlib.sha256(audio_file.read_bytes()).hexdigest()
    if sha != metadata.get("sha256"):
        raise FishAudioWorkflowError("Completed Fish Audio bytes do not match their SHA-256")
    return {
        "status": "completed",
        "provider": "ai33",
        "voice_source": "fishaudio",
        "voice_id": state["voice_id"],
        "task_id": state["task_id"],
        "run_id": state["run_id"],
        "file": audio_file.relative_to(output).as_posix(),
        "sha256": sha,
        "format": metadata["format"],
        "bytes": metadata["bytes"],
        "credit_cost": state.get("credit_cost"),
        "director_model": state["director_model"],
    }


async def generate_fish_audio(
    script: str,
    output: Path,
    *,
    director_model: str,
    provider: StructuredTextProvider | None,
    ai33_api_key: str | None,
    voice_id: str = _DEFAULT_VOICE_ID,
    speed: float = 1.0,
    poll_timeout_seconds: int = 3600,
    poll_interval_seconds: float = 8.0,
    resume: bool = False,
    speech_client: AI33SpeechClient | None = None,
) -> dict[str, Any]:
    """Run a new Fish director or resume a known TTS task without a second POST."""
    if not isinstance(script, str) or not script.strip():
        raise FishAudioWorkflowError("The source script must not be empty")
    if not voice_id.startswith("fishaudio_"):
        raise FishAudioWorkflowError("Fish voice ID is not a Fish Audio source ID")
    source_sha = hashlib.sha256(script.encode("utf-8")).hexdigest()
    pending = unfinished_audio_runs(output)

    if resume:
        if len(pending) != 1:
            raise FishAudioWorkflowError(
                "--resume-audio requires exactly one incomplete audio run"
            )
        state_path = pending[0]
        directory = state_path.parent
        state = _read_json(state_path)
        if state.get("source_sha256") != source_sha:
            raise FishAudioWorkflowError("Audio source differs from the saved run script")
        if state.get("voice_id") != voice_id:
            raise FishAudioWorkflowError("Voice differs from the saved TTS task")
        if not (directory / "output.json").is_file():
            raise FishAudioWorkflowError(
                "Fish director did not finish; use --regenerate-audio only "
                "after reconciling any uncertain submissions"
            )
    else:
        if pending:
            raise FishAudioWorkflowError(
                "A previous TTS task is incomplete. Use --resume-audio or "
                "reconcile its submission before requesting paid regeneration."
            )
        directory = output / "audio" / "runs" / uuid4().hex
        directory.mkdir(parents=True, exist_ok=False)
        state_path = directory / "task.json"
        state = {
            "run_id": directory.name,
            "source_sha256": source_sha,
            "director_model": director_model,
            "director_prompt_sha256": hashlib.sha256(fish_prompt_bytes()).hexdigest(),
            "voice_id": voice_id,
            "speed": speed,
            "status": "director_running",
        }
        _atomic_json(directory / "input.json", {"plain_script_for_recording": script})
        _atomic_json(state_path, state)
        if provider is None:
            raise FishAudioWorkflowError("OPENAI_API_KEY is required for the Fish director")
        try:
            directed, response = await run_fish_director(
                script, provider=provider, model=director_model
            )
        except FishAudioScriptError:
            # A rejected director output cannot have submitted any TTS request.
            # An explicit regeneration may safely request a corrected director.
            state["status"] = "director_invalid"
            _atomic_json(state_path, state)
            raise
        except Exception:
            state["status"] = "director_unknown"
            _atomic_json(state_path, state)
            raise
        _atomic_json(directory / "output.json", directed.model_dump())
        usage = getattr(response, "usage", None)
        if usage is not None:
            state["director_usage"] = (
                usage.model_dump(mode="json")
                if callable(getattr(usage, "model_dump", None))
                else None
            )
        state["status"] = "director_completed"
        _atomic_json(state_path, state)

    if speech_client is None:
        if not ai33_api_key:
            raise FishAudioWorkflowError(
                "AI33_API_KEY is required to submit or resume Fish Audio TTS"
            )
        speech_client = AI33SpeechClient(ai33_api_key)

    if state.get("status") in {"tts_submitting_unknown", "director_unknown"}:
        raise FishAudioWorkflowError(
            "A paid submission has an unknown outcome; do not resend automatically. "
            f"Inspect {state_path}"
        )
    directed_text = _read_json(directory / "output.json")["plain_script_for_recording"]
    if not isinstance(directed_text, str):
        raise FishAudioWorkflowError("Saved Fish director output is malformed")

    if not state.get("task_id"):
        state["status"] = "tts_submitting_unknown"
        _atomic_json(state_path, state)
        try:
            created = await asyncio.to_thread(
                speech_client.create,
                text=directed_text,
                voice_id=voice_id,
                speed=speed,
            )
        except Exception as exc:
            raise FishAudioWorkflowError(
                f"OpenSpeaker TTS submission outcome unknown. Inspect {state_path}; "
                "never automatically repeat the paid POST."
            ) from exc
        state["create_response"] = created
        task_id = task_id_of(created)
        if not task_id:
            _atomic_json(state_path, state)
            raise FishAudioWorkflowError(
                "OpenSpeaker TTS returned no task_id; reconcile its task history "
                "before attempting another generation"
            )
        state["task_id"] = task_id
        state["status"] = "submitted"
        _atomic_json(state_path, state)

    result = state.get("last_poll")
    if not isinstance(result, dict) or str(result.get("status", "")).lower() not in {
        "done", "completed", "success",
    }:
        result = await asyncio.to_thread(
            speech_client.poll,
            state["task_id"],
            timeout_seconds=poll_timeout_seconds,
            interval_seconds=poll_interval_seconds,
        )
        state["last_poll"] = result
        state["status"] = "download_pending"
        _atomic_json(state_path, state)

    url = audio_url_of(result)
    if not url:
        raise AI33SpeechError(
            f"TTS task {state['task_id']} finished without a recognized audio URL. "
            f"Inspect {state_path}; the paid task must not be recreated."
        )
    audio = await asyncio.to_thread(speech_client.download_audio, url, directory)
    state["audio"] = audio
    state["credit_cost"] = result.get("credit_cost")
    state["status"] = "completed"
    _atomic_json(state_path, state)
    artifact = _audio_result(output, directory, state)
    _atomic_json(output / "audio" / "latest.json", artifact)
    return artifact
