"""Independent per-block Fish director and AI33 Pro speech with durable paid tasks."""

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
from ai_video_factory.providers.avatar_voice import valid_voice_provider
from ai_video_factory.providers.base import StructuredTextProvider
from ai_video_factory.providers.openai_stt import OpenAISttClient, OpenAISttError

from .audio_join import join_audio_blocks
from .contracts import B11Output, B12Input
from .fish_audio import (
    FishAudioScriptError,
    fish_prompt_bytes,
    run_fish_director,
    validate_fish_script,
)
from .validation_retries import MAX_VALIDATION_RETRIES, is_schema_failure, validation_feedback
from .workflow import materialize_blocks

_DEFAULT_VOICE_ID = "fishaudio_80e34d5e0b2b4577a486f3a77e357261"


class FishAudioWorkflowError(RuntimeError):
    """Speech state requires recovery or its saved source no longer matches."""


class SttRetryLimitReached(FishAudioWorkflowError):
    """A block has already used two STT attempts for this audio version."""


class AudioRegenerationLimitReached(FishAudioWorkflowError):
    """A block has already used all three audio regenerations."""


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
    """Never replace an output while a block could still have a paid task."""
    folder = output / "audio" / "runs"
    if folder.is_symlink():
        raise FishAudioWorkflowError("Linked Fish Audio runs directory")
    pending = []
    for state_path in sorted(folder.glob("*/task.json")):
        state = _read_json(state_path)
        if state.get("status") not in {"completed", "director_invalid"}:
            pending.append(state_path)
    return pending


def _inputs(script: str, b11: B11Output) -> list[B12Input]:
    if b11.error is not None or b11.blocks is None or b11.narrative_core is None:
        raise FishAudioWorkflowError("Fish Audio requires a validated B1.1 checkpoint")
    blocks = materialize_blocks(script, b11.blocks)
    if "".join(block.text for block in blocks) != script:
        raise FishAudioWorkflowError("The ordered audio blocks do not reconstruct the source")
    return [
        B12Input(
            narrative_core=b11.narrative_core,
            current_block_id=block.block_id,
            blocks=(block,),
        )
        for block in blocks
    ]


def _block_audio(output: Path, directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    metadata = state.get("audio")
    if not isinstance(metadata, dict):
        raise FishAudioWorkflowError("Completed Fish block has no audio metadata")
    name = metadata.get("file")
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise FishAudioWorkflowError("Invalid Fish block audio filename")
    audio_file = directory / name
    if audio_file.is_symlink() or not audio_file.is_file():
        raise FishAudioWorkflowError("Completed Fish block audio is missing or linked")
    sha = hashlib.sha256(audio_file.read_bytes()).hexdigest()
    if sha != metadata.get("sha256"):
        raise FishAudioWorkflowError("Completed Fish block bytes do not match SHA-256")
    return {
        "block_id": state["block_id"],
        "task_id": state["task_id"],
        "file": audio_file.relative_to(output).as_posix(),
        "sha256": sha,
        "format": metadata["format"],
        "bytes": metadata["bytes"],
        "credit_cost": state.get("credit_cost"),
        "director_usage": state.get("director_usage"),
    }


async def _transcribe_block(
    item: dict[str, Any],
    *,
    output: Path,
    run_dir: Path,
    stt_client: OpenAISttClient,
) -> dict[str, Any]:
    """Transcribe an individual completed TTS block; never repeat an uncertain POST."""
    directory = run_dir / "blocks" / f"block_{item['block_id']}"
    transcript_path = directory / "stt.json"
    request_path = directory / "stt_request.json"
    raw_response_path = directory / "stt_response.json"
    audio_path = output / item["file"]
    if (
        transcript_path.is_symlink() or request_path.is_symlink()
        or raw_response_path.is_symlink()
    ):
        raise FishAudioWorkflowError("Refusing linked Fish STT state")
    model = stt_client.model
    if transcript_path.is_file():
        cached = _read_json(transcript_path)
        if (
            cached.get("source_audio_sha256") != item["sha256"]
            or cached.get("model") != model
            or not isinstance(cached.get("words"), list)
            or not cached["words"]
        ):
            raise FishAudioWorkflowError(
                f"Saved STT differs from TTS block {item['block_id']}"
            )
    else:
        if request_path.is_file():
            previous = _read_json(request_path)
            if (
                previous.get("status") == "request_started_unknown"
                and not raw_response_path.is_file()
            ):
                raise FishAudioWorkflowError(
                    f"OpenAI STT submission for block {item['block_id']} may be billed; "
                    f"reconcile {request_path} before retrying"
                )
            if previous.get("status") == "completed":
                raise FishAudioWorkflowError(
                    f"Saved STT transcript missing for block {item['block_id']}; "
                    "never repeat a completed paid request"
                )
            if (
                previous.get("source_audio_sha256") != item["sha256"]
                or previous.get("model") != model
            ):
                raise FishAudioWorkflowError("Saved STT request differs from source audio")
        _atomic_json(request_path, {
            "status": "request_started_unknown",
            "source_audio_sha256": item["sha256"],
            "model": model,
        })
        try:
            if isinstance(stt_client, OpenAISttClient):
                transcript = await asyncio.to_thread(
                    stt_client.transcribe_durable, audio_path,
                    response_path=raw_response_path,
                    source_audio_sha256=item["sha256"],
                )
            else:
                transcript = await asyncio.to_thread(stt_client.transcribe, audio_path)
        except OpenAISttError as exc:
            # The STT response was received but failed local validation. This
            # is retryable; it is not an ambiguous paid POST outcome.
            _atomic_json(request_path, {
                "status": "response_invalid",
                "source_audio_sha256": item["sha256"],
                "model": model,
                "error": str(exc),
            })
            raise
        if transcript.get("model") != model:
            raise FishAudioWorkflowError("OpenAI STT returned an unexpected model")
        cached = {**transcript, "source_audio_sha256": item["sha256"]}
        _atomic_json(transcript_path, cached)
        _atomic_json(request_path, {
            "status": "completed",
            "source_audio_sha256": item["sha256"],
            "model": model,
        })
    stt_sha = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
    return {
        **item,
        "stt": {
            "file": transcript_path.relative_to(output).as_posix(),
            "sha256": stt_sha,
            "model": model,
            "word_count": len(cached["words"]),
        },
    }


async def retranscribe_audio_block(
    output: Path,
    audio_artifact: dict[str, Any],
    block_id: int,
    *,
    stt_client: OpenAISttClient,
) -> int:
    """Retry STT on the same saved block audio and durably record each attempt."""
    run_id = audio_artifact.get("run_id")
    if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id:
        raise FishAudioWorkflowError("Audio artifact has an invalid run ID for STT retry")
    run_dir = output / "audio" / "runs" / run_id
    task_path = run_dir / "task.json"
    task = _read_json(task_path)
    if task.get("status") != "completed" or task.get("audio") != audio_artifact:
        raise FishAudioWorkflowError("Saved audio run is not complete or has changed")
    records = audio_artifact.get("blocks")
    if not isinstance(records, list):
        raise FishAudioWorkflowError("Completed audio artifact has no block records")
    matches = [item for item in records if item.get("block_id") == block_id]
    if len(matches) != 1:
        raise FishAudioWorkflowError(f"Audio block {block_id} is missing or duplicated")
    item = matches[0]
    transcript_ref = item.get("stt")
    if not isinstance(transcript_ref, dict):
        raise FishAudioWorkflowError(f"Audio block {block_id} has no saved STT reference")
    transcript_name = transcript_ref.get("file")
    if not isinstance(transcript_name, str):
        raise FishAudioWorkflowError(f"Audio block {block_id} has an invalid STT path")
    transcript_path = output / transcript_name
    state_dir = (
        transcript_path.parent.parent
        if transcript_path.parent.name == "stt_attempts"
        else transcript_path.parent
    )
    retry_path = state_dir / "stt_retry_state.json"
    attempt_dir = state_dir / "stt_attempts"
    latest_path = output / "audio" / "latest.json"
    if (
        state_dir.is_symlink() or retry_path.is_symlink() or attempt_dir.is_symlink()
        or latest_path.is_symlink()
        or transcript_path.is_symlink() or not transcript_path.is_file()
    ):
        raise FishAudioWorkflowError(f"Refusing linked or missing STT state for block {block_id}")
    source_sha = item.get("sha256")
    original = _read_json(transcript_path)
    if original.get("source_audio_sha256") != source_sha:
        raise FishAudioWorkflowError(f"Saved STT source differs for block {block_id}")

    if retry_path.is_file():
        retry_state = _read_json(retry_path)
        if (
            retry_state.get("schema_version") != "fish-stt-retries-v1"
            or retry_state.get("source_audio_sha256") != source_sha
            or retry_state.get("model") != stt_client.model
        ):
            raise FishAudioWorkflowError(f"Saved STT retry state differs for block {block_id}")
        attempts = retry_state.get("attempts")
        if not isinstance(attempts, list):
            raise FishAudioWorkflowError(f"Invalid STT retry history for block {block_id}")
        if any(entry.get("status") == "request_started_unknown" for entry in attempts):
            raise FishAudioWorkflowError(
                f"STT retry for block {block_id} has an uncertain paid outcome; "
                f"reconcile {retry_path} before another attempt"
            )
    else:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        first_path = attempt_dir / "attempt_1.json"
        _atomic_json(first_path, original)
        first_sha = hashlib.sha256(first_path.read_bytes()).hexdigest()
        attempts = [{
            "attempt": 1,
            "status": "completed",
            "file": first_path.relative_to(output).as_posix(),
            "sha256": first_sha,
            "word_count": len(original.get("words", [])),
        }]
        retry_state = {
            "schema_version": "fish-stt-retries-v1",
            "block_id": block_id,
            "source_audio_sha256": source_sha,
            "model": stt_client.model,
            "attempts": attempts,
        }

    used_numbers = [
        entry.get("attempt") for entry in attempts
        if isinstance(entry.get("attempt"), int)
    ]
    attempt_number = max(used_numbers, default=1) + 1
    if attempt_number > 2:
        raise SttRetryLimitReached(
            f"STT block {block_id} already used both attempts for this audio"
        )
    retry_state["attempts"] = [
        *attempts,
        {"attempt": attempt_number, "status": "request_started_unknown"},
    ]
    _atomic_json(retry_path, retry_state)

    audio_name = item.get("file")
    if not isinstance(audio_name, str):
        raise FishAudioWorkflowError(f"Audio block {block_id} has no file path")
    audio_path = output / audio_name
    try:
        transcript = await asyncio.to_thread(stt_client.transcribe, audio_path)
    except OpenAISttError as exc:
        retry_state["attempts"] = [
            *attempts,
            {
                "attempt": attempt_number,
                "status": "response_invalid",
                "error": str(exc),
            },
        ]
        _atomic_json(retry_path, retry_state)
        raise
    if transcript.get("model") != stt_client.model:
        raise FishAudioWorkflowError("OpenAI STT retry returned an unexpected model")
    retried = {**transcript, "source_audio_sha256": source_sha}
    attempt_dir.mkdir(parents=True, exist_ok=True)
    attempt_path = attempt_dir / f"attempt_{attempt_number}.json"
    _atomic_json(attempt_path, retried)
    attempt_sha = hashlib.sha256(attempt_path.read_bytes()).hexdigest()
    completed_attempt = {
        "attempt": attempt_number,
        "status": "completed",
        "file": attempt_path.relative_to(output).as_posix(),
        "sha256": attempt_sha,
        "word_count": len(retried["words"]),
    }
    retry_state["attempts"] = [*attempts, completed_attempt]
    _atomic_json(retry_path, retry_state)

    item["stt"] = {
        "file": attempt_path.relative_to(output).as_posix(),
        "sha256": attempt_sha,
        "model": stt_client.model,
        "word_count": len(retried["words"]),
    }
    task["audio"] = audio_artifact
    _atomic_json(task_path, task)
    _atomic_json(latest_path, audio_artifact)
    return attempt_number


async def _transcribe_regenerated_block(
    output: Path,
    audio_artifact: dict[str, Any],
    item: dict[str, Any],
    *,
    stt_client: OpenAISttClient,
) -> None:
    audio_path = output / item["file"]
    directory = audio_path.parent
    transcript_path = directory / "stt.json"
    request_path = directory / "stt_request.json"
    if any(path.is_symlink() for path in (directory, transcript_path, request_path)):
        raise FishAudioWorkflowError("Refusing linked regenerated STT state")
    source_sha = item["sha256"]
    if request_path.is_file():
        request_state = _read_json(request_path)
        if request_state.get("status") == "request_started_unknown":
            raise FishAudioWorkflowError(
                f"Regenerated STT for block {item['block_id']} has an uncertain outcome; "
                f"reconcile {request_path} before retrying"
            )
    _atomic_json(request_path, {
        "status": "request_started_unknown",
        "source_audio_sha256": source_sha,
        "model": stt_client.model,
    })
    try:
        transcript = await asyncio.to_thread(stt_client.transcribe, audio_path)
    except OpenAISttError as exc:
        _atomic_json(request_path, {
            "status": "response_invalid",
            "source_audio_sha256": source_sha,
            "model": stt_client.model,
            "error": str(exc),
        })
        raise
    if transcript.get("model") != stt_client.model:
        raise FishAudioWorkflowError("OpenAI STT returned an unexpected model")
    saved = {**transcript, "source_audio_sha256": source_sha}
    _atomic_json(transcript_path, saved)
    _atomic_json(request_path, {
        "status": "completed",
        "source_audio_sha256": source_sha,
        "model": stt_client.model,
    })
    item["stt"] = {
        "file": transcript_path.relative_to(output).as_posix(),
        "sha256": hashlib.sha256(transcript_path.read_bytes()).hexdigest(),
        "model": stt_client.model,
        "word_count": len(saved["words"]),
    }
    audio_artifact["stt_complete"] = all(
        isinstance(block.get("stt"), dict) for block in audio_artifact["blocks"]
    )
    run_dir = output / "audio" / "runs" / audio_artifact["run_id"]
    task_path = run_dir / "task.json"
    task = _read_json(task_path)
    task["audio"] = audio_artifact
    _atomic_json(task_path, task)
    latest_path = output / "audio" / "latest.json"
    if latest_path.is_symlink():
        raise FishAudioWorkflowError("Refusing linked audio/latest.json")
    _atomic_json(latest_path, audio_artifact)


async def regenerate_audio_block(
    output: Path,
    audio_artifact: dict[str, Any],
    block_id: int,
    *,
    speech_client: AI33SpeechClient,
    stt_client: OpenAISttClient,
    poll_timeout_seconds: int = 3600,
    poll_interval_seconds: float = 8.0,
) -> int:
    """Regenerate just one Fish TTS block, then run its first STT attempt."""
    run_id = audio_artifact.get("run_id")
    if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id:
        raise FishAudioWorkflowError("Audio artifact has an invalid run ID")
    run_dir = output / "audio" / "runs" / run_id
    run_task_path = run_dir / "task.json"
    latest_path = output / "audio" / "latest.json"
    if latest_path.is_symlink():
        raise FishAudioWorkflowError("Refusing linked audio/latest.json")
    run_task = _read_json(run_task_path)
    if run_task.get("status") != "completed" or run_task.get("audio") != audio_artifact:
        raise FishAudioWorkflowError("Saved audio run is not complete or has changed")
    records = audio_artifact.get("blocks")
    if not isinstance(records, list):
        raise FishAudioWorkflowError("Completed audio artifact has no blocks")
    matches = [item for item in records if item.get("block_id") == block_id]
    if len(matches) != 1:
        raise FishAudioWorkflowError(f"Audio block {block_id} is missing or duplicated")
    item = matches[0]
    block_dir = run_dir / "blocks" / f"block_{block_id}"
    block_state_path = block_dir / "task.json"
    block_state = _read_json(block_state_path)
    directed = _read_json(block_dir / "output.json")
    directed_text = directed.get("plain_script_for_recording")
    if not isinstance(directed_text, str) or not directed_text.strip():
        raise FishAudioWorkflowError(f"Block {block_id} has no saved Fish recording text")

    regen_path = block_dir / "audio_regeneration_state.json"
    if regen_path.is_symlink():
        raise FishAudioWorkflowError("Refusing linked audio regeneration state")
    if regen_path.is_file():
        regen_state = _read_json(regen_path)
        if regen_state.get("source_sha256") != block_state.get("source_sha256"):
            raise FishAudioWorkflowError("Block source changed since audio regeneration")
        attempts = regen_state.get("attempts")
        if not isinstance(attempts, list):
            raise FishAudioWorkflowError("Invalid audio regeneration history")
    else:
        attempts = []
        regen_state = {
            "schema_version": "fish-audio-regenerations-v1",
            "block_id": block_id,
            "source_sha256": block_state.get("source_sha256"),
            "attempts": attempts,
        }

    attempt = attempts[-1] if attempts else None
    if attempt is not None and attempt.get("status") == "completed":
        previous_audio = attempt.get("audio")
        if isinstance(previous_audio, dict) and item.get("file") == previous_audio.get("file"):
            attempt = None  # The current version has already failed its STT allowance.
        elif isinstance(previous_audio, dict):
            # Recover a completed paid TTS task if the process stopped before activation.
            regenerate_number = attempt["attempt"]
            attempt_dir = block_dir / "audio_regenerations" / f"attempt_{regenerate_number}"
        else:
            raise FishAudioWorkflowError("Completed audio regeneration has no audio artifact")
    if attempt is not None and attempt.get("status") == "request_started_unknown":
        raise FishAudioWorkflowError(
            f"Audio regeneration for block {block_id} has an uncertain paid POST; "
            f"reconcile {regen_path} before retrying"
        )

    if attempt is None:
        regenerate_number = len(attempts) + 1
        if regenerate_number > 3:
            raise AudioRegenerationLimitReached(
                f"Block {block_id} already used all three audio regenerations"
            )
        regen_root = block_dir / "audio_regenerations"
        attempt_dir = regen_root / f"attempt_{regenerate_number}"
        if regen_root.is_symlink() or attempt_dir.is_symlink():
            raise FishAudioWorkflowError("Refusing linked audio regeneration directory")
        attempt_dir.mkdir(parents=True, exist_ok=True)
        attempt = {
            "attempt": regenerate_number,
            "status": "tts_submitting_unknown",
            "directory": attempt_dir.relative_to(output).as_posix(),
        }
        attempts = [*attempts, attempt]
        regen_state["attempts"] = attempts
        _atomic_json(regen_path, regen_state)
        try:
            created = await asyncio.to_thread(
                speech_client.create,
                text=directed_text,
                voice_id=audio_artifact["voice_id"],
                speed=block_state["speed"],
            )
        except Exception as exc:
            raise FishAudioWorkflowError(
                f"Audio regeneration POST outcome is unknown for block {block_id}; "
                f"reconcile {regen_path} before retrying"
            ) from exc
        task_id = task_id_of(created)
        if not task_id:
            _atomic_json(regen_path, regen_state)
            raise FishAudioWorkflowError(
                f"Regenerated TTS for block {block_id} returned no task ID"
            )
        attempt["create_response"] = created
        attempt["task_id"] = task_id
        attempt["status"] = "submitted"
        _atomic_json(regen_path, regen_state)
    else:
        regenerate_number = attempt["attempt"]
        regen_root = block_dir / "audio_regenerations"
        attempt_dir = regen_root / f"attempt_{regenerate_number}"
        if regen_root.is_symlink() or attempt_dir.is_symlink():
            raise FishAudioWorkflowError("Refusing linked audio regeneration directory")
        attempt_dir.mkdir(parents=True, exist_ok=True)

    if attempt.get("status") in {"submitted", "download_pending"}:
        result = attempt.get("last_poll")
        if not isinstance(result, dict) or str(result.get("status", "")).lower() not in {
            "done", "completed", "success",
        }:
            result = await asyncio.to_thread(
                speech_client.poll,
                attempt["task_id"],
                timeout_seconds=poll_timeout_seconds,
                interval_seconds=poll_interval_seconds,
            )
            attempt["last_poll"] = result
            attempt["status"] = "download_pending"
            _atomic_json(regen_path, regen_state)
        url = audio_url_of(result)
        if not url:
            raise AI33SpeechError(
                f"Regenerated TTS block {block_id} has no audio URL; inspect {regen_path}"
            )
        audio_meta = await asyncio.to_thread(
            speech_client.download_audio, url, attempt_dir
        )
        attempt["audio"] = {
            **audio_meta,
            "file": (attempt_dir / audio_meta["file"]).relative_to(output).as_posix(),
        }
        attempt["credit_cost"] = result.get("credit_cost")
        attempt["status"] = "completed"
        _atomic_json(regen_path, regen_state)
    elif attempt.get("status") != "completed":
        raise FishAudioWorkflowError(
            f"Unrecognized audio regeneration state for block {block_id}: "
            f"{attempt.get('status')}"
        )

    version_audio = attempt.get("audio")
    if not isinstance(version_audio, dict):
        raise FishAudioWorkflowError("Completed audio regeneration is missing its file")
    audio_file = output / version_audio["file"]
    if audio_file.is_symlink() or not audio_file.is_file():
        raise FishAudioWorkflowError("Regenerated block audio is missing or linked")
    with audio_file.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != version_audio["sha256"]:
            raise FishAudioWorkflowError("Regenerated block audio failed SHA-256 verification")

    item.update({
        "task_id": attempt["task_id"],
        "file": version_audio["file"],
        "sha256": version_audio["sha256"],
        "format": version_audio["format"],
        "bytes": version_audio["bytes"],
        "credit_cost": attempt.get("credit_cost"),
    })
    item.pop("stt", None)
    audio_artifact["stt_complete"] = False
    narration_path = run_dir / f"narration_audio_regen_{regenerate_number}.wav"
    if narration_path.is_symlink():
        raise FishAudioWorkflowError("Refusing linked regenerated narration")
    merged = await asyncio.to_thread(
        join_audio_blocks,
        [output / block.get("file", "") for block in records],
        narration_path,
    )
    credit_costs = [block.get("credit_cost") for block in records]
    audio_artifact.update({
        "file": narration_path.relative_to(output).as_posix(),
        "sha256": merged["sha256"],
        "format": merged["format"],
        "bytes": merged["bytes"],
        "duration_seconds": merged["duration_seconds"],
        "block_start_seconds": merged["block_start_seconds"],
        "block_end_seconds": merged["block_end_seconds"],
        "block_durations_seconds": merged["block_durations_seconds"],
        "credit_cost": (
            sum(credit_costs)
            if all(isinstance(value, (int, float)) for value in credit_costs)
            else None
        ),
    })
    attempt["active"] = True
    _atomic_json(regen_path, regen_state)
    run_task["audio"] = audio_artifact
    _atomic_json(run_task_path, run_task)
    _atomic_json(latest_path, audio_artifact)
    await _transcribe_regenerated_block(
        output, audio_artifact, item, stt_client=stt_client
    )
    return regenerate_number


async def _generate_block(
    payload: B12Input,
    *,
    output: Path,
    run_dir: Path,
    provider: StructuredTextProvider | None,
    speech_client: AI33SpeechClient,
    director_model: str,
    voice_id: str,
    speed: float,
    poll_timeout_seconds: int,
    poll_interval_seconds: float,
    voice_provider: str = "fishaudio",
) -> dict[str, Any]:
    block = payload.blocks[0]
    directory = run_dir / "blocks" / f"block_{block.block_id}"
    state_path = directory / "task.json"
    input_data = json.loads(payload.model_dump_json())
    source_sha = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
    if state_path.exists():
        state = _read_json(state_path)
        if (
            state.get("block_id") != block.block_id
            or state.get("source_sha256") != source_sha
            or state.get("voice_provider", "fishaudio") != voice_provider
            or _read_json(directory / "input.json") != input_data
        ):
            raise FishAudioWorkflowError(f"Saved Fish input differs for block {block.block_id}")
    else:
        directory.mkdir(parents=True, exist_ok=False)
        state = {
            "block_id": block.block_id,
            "source_sha256": source_sha,
            "director_model": director_model,
            "voice_id": voice_id,
            "voice_provider": voice_provider,
            "speed": speed,
            "status": "director_running" if voice_provider == "fishaudio" else "text_ready",
        }
        _atomic_json(directory / "input.json", input_data)
        _atomic_json(state_path, state)

    if (directory / "output.json").exists():
        directed = _read_json(directory / "output.json")
        directed_text = directed.get("plain_script_for_recording")
        if not isinstance(directed_text, str):
            raise FishAudioWorkflowError(f"Malformed saved Fish output for block {block.block_id}")
        if voice_provider == "fishaudio":
            validate_fish_script(block.text, directed_text)
        elif directed_text != block.text:
            raise FishAudioWorkflowError("Non-Fish TTS must preserve the raw spoken block")
    else:
        if state.get("task_id") or state.get("status") == "tts_submitting_unknown":
            raise FishAudioWorkflowError("Paid Fish task has no durable director output")
        if voice_provider != "fishaudio":
            # Never run or bill the Fish director on MiniMax / ElevenLabs.
            directed_text = block.text
            _atomic_json(
                directory / "output.json",
                {"plain_script_for_recording": directed_text},
            )
            state["status"] = "director_skipped"
            _atomic_json(state_path, state)
        else:
            if provider is None:
                raise FishAudioWorkflowError(
                    f"Block {block.block_id} has no completed director; "
                    "resume requires a validated saved output"
                )
            state["status"] = "director_running"
            _atomic_json(state_path, state)
            director_attempts = state.get("director_attempts_used", 0)
            if not isinstance(director_attempts, int) or director_attempts < 0:
                raise FishAudioWorkflowError("Invalid saved Fish director attempt count")
            if director_attempts >= MAX_VALIDATION_RETRIES + 1:
                raise FishAudioWorkflowError(
                    f"Fish director block {block.block_id} has exhausted its "
                    "two validation retries; manually reconcile this block"
                )
            previous_response_id: str | None = (
                (state.get("director_response") or {}).get("id")
                if getattr(provider, "supports_response_continuation", False)
                else None
            )
            feedback = ""
            if director_attempts and state.get("status") == "director_invalid":
                # A resume after a saved validation failure continues the same
                # conversation and spends only the remaining block attempts.
                rejected_path = directory / f"rejected_attempt_{director_attempts}.json"
                rejected = _read_json(rejected_path)
                previous = rejected.get("response")
                if not isinstance(previous, dict) or previous.get("unparsed_response"):
                    previous = None
                feedback = validation_feedback(
                    "Fish Audio",
                    FishAudioScriptError(str(rejected["validation_error"])),
                    block_id=block.block_id,
                    previous=previous,
                    continued=bool(previous_response_id),
                    instructions=(
                        "Repair this one directed block. Preserve all source words "
                        "and their order; insert only correctly positioned Fish "
                        "tags, never inside words or after the final spoken word."
                    ),
                )
            while director_attempts < MAX_VALIDATION_RETRIES + 1:
                director_attempts += 1
                candidate: dict[str, Any] | None = None
                state["director_attempts_used"] = director_attempts
                state["status"] = "director_running"
                _atomic_json(state_path, state)

                def save_director_response(
                    response: Any, attempt_number: int = director_attempts,
                ) -> None:
                    usage = getattr(response, "usage", None)
                    usage_data = (
                        usage.model_dump(mode="json")
                        if usage is not None and callable(getattr(usage, "model_dump", None))
                        else usage if isinstance(usage, dict) else None
                    )
                    record = {
                        "attempt": attempt_number,
                        "id": getattr(response, "id", None),
                        "model": getattr(response, "model", None),
                        "service_tier": getattr(response, "service_tier", None),
                        "usage": usage_data,
                    }
                    history = state.setdefault("director_responses", [])
                    if not isinstance(history, list):
                        raise FishAudioWorkflowError("Malformed saved director accounting history")
                    history.append(record)
                    state["director_usage"] = usage_data
                    state["director_response"] = record
                    _atomic_json(state_path, state)

                def save_candidate(
                    output: Any, attempt_number: int = director_attempts,
                ) -> None:
                    nonlocal candidate
                    candidate = output.model_dump()
                    _atomic_json(
                        directory / f"director_candidate_{attempt_number}.json",
                        candidate,
                    )

                try:
                    directed, _response = await run_fish_director(
                        payload, provider=provider, model=director_model,
                        on_response=save_director_response,
                        on_candidate=save_candidate,
                        previous_response_id=previous_response_id if feedback else None,
                        correction_feedback=feedback,
                    )
                except Exception as error:
                    if not isinstance(error, FishAudioScriptError) and not is_schema_failure(error):
                        state["status"] = "director_unknown"
                        _atomic_json(state_path, state)
                        raise
                    _atomic_json(
                        directory / f"rejected_attempt_{director_attempts}.json",
                        {
                            "attempt": director_attempts,
                            "validation_error": str(error),
                            "response": candidate if candidate is not None else {
                                "unparsed_response": True,
                            },
                        },
                    )
                    state["status"] = "director_invalid"
                    state.setdefault("director_validation_errors", []).append(str(error))
                    _atomic_json(state_path, state)
                    if director_attempts > MAX_VALIDATION_RETRIES:
                        raise FishAudioScriptError(
                            f"Fish director block {block.block_id} failed validation "
                            f"after {MAX_VALIDATION_RETRIES} retries: {error}"
                        ) from error
                    previous_response_id = (
                        (state.get("director_response") or {}).get("id")
                        if getattr(provider, "supports_response_continuation", False)
                        else None
                    )
                    feedback = validation_feedback(
                        "Fish Audio", error, block_id=block.block_id,
                        previous=candidate,
                        continued=bool(previous_response_id),
                        instructions=(
                            "Repair only the Fish director text for this block. "
                            "Do not add, remove, replace or reorder any spoken words. "
                            "Only insert valid, well-positioned Fish emotion/delivery "
                            "tags without splitting words or placing tags after "
                            "the final spoken word. Preserve the original script "
                            "apart from permitted layout/punctuation differences. "
                            "Return the full directed block in the required schema."
                        ),
                    )
                else:
                    break
            directed_text = directed.plain_script_for_recording
            _atomic_json(directory / "output.json", directed.model_dump())
            state["status"] = "director_completed"
            _atomic_json(state_path, state)

    if state.get("status") == "completed":
        return _block_audio(output, directory, state)
    if state.get("status") == "tts_submitting_unknown":
        raise FishAudioWorkflowError(
            f"Block {block.block_id} has an unknown paid submission; inspect {state_path}"
        )
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
                f"OpenSpeaker TTS submission outcome unknown for block {block.block_id}; "
                f"inspect {state_path}, never automatically repeat the paid POST."
            ) from exc
        state["create_response"] = created
        task_id = task_id_of(created)
        if not task_id:
            _atomic_json(state_path, state)
            raise FishAudioWorkflowError(
                f"Block {block.block_id} received no task_id; reconcile the paid task first"
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
            f"TTS block {block.block_id} task {state['task_id']} has no recognized audio URL; "
            f"inspect {state_path} without recreating the paid task"
        )
    audio = await asyncio.to_thread(speech_client.download_audio, url, directory)
    state["audio"] = audio
    state["credit_cost"] = result.get("credit_cost")
    state["status"] = "completed"
    _atomic_json(state_path, state)
    return _block_audio(output, directory, state)


async def generate_fish_audio(
    script: str,
    output: Path,
    *,
    b11: B11Output,
    director_model: str,
    provider: StructuredTextProvider | None,
    ai33_api_key: str | None,
    voice_id: str = _DEFAULT_VOICE_ID,
    speed: float = 0.9,
    max_parallel_calls: int = 8,
    poll_timeout_seconds: int = 3600,
    poll_interval_seconds: float = 8.0,
    voice_provider: str = "fishaudio",
    resume: bool = False,
    speech_client: AI33SpeechClient | None = None,
    stt_client: OpenAISttClient | None = None,
) -> dict[str, Any]:
    """Run each block independently, then publish a single ordered WAV narration."""
    if not isinstance(script, str) or not script.strip():
        raise FishAudioWorkflowError("The source script must not be empty")
    if (
        not isinstance(voice_id, str)
        or not voice_id.strip()
        or any(ch.isspace() for ch in voice_id)
    ):
        raise FishAudioWorkflowError("AI33 voice ID is missing or invalid")
    valid_voice_provider(voice_provider)
    if max_parallel_calls < 1:
        raise FishAudioWorkflowError("max_parallel_calls must be positive")
    inputs = _inputs(script, b11)
    source_sha = hashlib.sha256(script.encode("utf-8")).hexdigest()
    inputs_sha = hashlib.sha256(
        json.dumps(
            [item.model_dump() for item in inputs],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    pending = unfinished_audio_runs(output)

    if resume:
        if len(pending) != 1:
            raise FishAudioWorkflowError(
                "--resume-audio requires exactly one incomplete audio run"
            )
        state_path = pending[0]
        run_dir = state_path.parent
        state = _read_json(state_path)
        if (
            state.get("source_sha256") != source_sha
            or state.get("inputs_sha256") != inputs_sha
            or state.get("voice_id") != voice_id
            or state.get("voice_provider", "fishaudio") != voice_provider
            or state.get("director_model") != director_model
            or state.get("director_prompt_sha256") != (
                hashlib.sha256(fish_prompt_bytes()).hexdigest()
                if voice_provider == "fishaudio" else None
            )
            or state.get("speed") != speed
        ):
            raise FishAudioWorkflowError("Saved Fish audio input, prompt or voice differs")
        # Never start new paid work when another block has an ambiguous POST.
        for path in sorted((run_dir / "blocks").glob("block_*/task.json")):
            block_state = _read_json(path)
            if block_state.get("status") == "tts_submitting_unknown":
                raise FishAudioWorkflowError(
                    f"Unknown paid submission at {path}; reconcile before resuming"
                )
    else:
        if pending:
            raise FishAudioWorkflowError(
                "A previous Fish TTS task is incomplete. Use --resume-audio or "
                "reconcile its paid submissions before regeneration."
            )
        run_dir = output / "audio" / "runs" / uuid4().hex
        run_dir.mkdir(parents=True, exist_ok=False)
        state_path = run_dir / "task.json"
        state = {
            "run_id": run_dir.name,
            "source_sha256": source_sha,
            "inputs_sha256": inputs_sha,
            "director_model": director_model,
            "director_prompt_sha256": (
                hashlib.sha256(fish_prompt_bytes()).hexdigest()
                if voice_provider == "fishaudio" else None
            ),
            "voice_provider": voice_provider,
            "voice_id": voice_id,
            "speed": speed,
            "block_ids": [item.current_block_id for item in inputs],
            "status": "running",
        }
        _atomic_json(state_path, state)

    if speech_client is None:
        if not ai33_api_key:
            raise FishAudioWorkflowError("AI33_API_KEY is required for Fish voice synthesis")
        speech_client = AI33SpeechClient(ai33_api_key)
    if provider is None and not resume and voice_provider == "fishaudio":
        raise FishAudioWorkflowError("OPENAI_API_KEY is required for the Fish director")

    semaphore = asyncio.Semaphore(max_parallel_calls)

    async def one(payload: B12Input) -> dict[str, Any]:
        async with semaphore:
            item = await _generate_block(
                payload,
                output=output,
                run_dir=run_dir,
                provider=provider,
                speech_client=speech_client,
                director_model=director_model,
                voice_id=voice_id,
                speed=speed,
                poll_timeout_seconds=poll_timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                voice_provider=voice_provider,
            )
            if stt_client is not None:
                item = await _transcribe_block(
                    item, output=output, run_dir=run_dir, stt_client=stt_client
                )
            return item

    # return_exceptions waits for ALL paid submissions and polls. An error in
    # one block must never cancel other blocks while their POST outcome is open.
    outcomes = await asyncio.gather(*(one(payload) for payload in inputs), return_exceptions=True)
    failures = [item for item in outcomes if isinstance(item, BaseException)]
    if failures:
        paid_states = [
            _read_json(path)
            for path in sorted((run_dir / "blocks").glob("block_*/task.json"))
        ]
        state["status"] = (
            "incomplete"
            if any(item.get("task_id") or item.get("status") == "tts_submitting_unknown"
                   for item in paid_states)
            else "director_invalid"
        )
        _atomic_json(state_path, state)
        if len(failures) == 1 and isinstance(
            failures[0], (FishAudioScriptError, FishAudioWorkflowError)
        ):
            raise failures[0]
        raise FishAudioWorkflowError(
            f"{len(failures)} Fish block(s) incomplete; inspect {state_path}"
        ) from failures[0]

    blocks = [item for item in outcomes if isinstance(item, dict)]
    if [item["block_id"] for item in blocks] != [item.current_block_id for item in inputs]:
        raise FishAudioWorkflowError("Fish block completion order differs from source order")
    try:
        merged = await asyncio.to_thread(
            join_audio_blocks,
            [output / item["file"] for item in blocks],
            run_dir / "narration.wav",
        )
    except Exception:
        state["status"] = "join_pending"
        _atomic_json(state_path, state)
        raise

    credits = [item["credit_cost"] for item in blocks]
    artifact: dict[str, Any] = {
        "status": "completed",
        "provider": "ai33",
        "voice_source": voice_provider,
        "voice_id": voice_id,
        "run_id": state["run_id"],
        "file": (run_dir / merged["file"]).relative_to(output).as_posix(),
        "sha256": merged["sha256"],
        "format": merged["format"],
        "bytes": merged["bytes"],
        "duration_seconds": merged["duration_seconds"],
        "pause_between_blocks_seconds": 0.5,
        "block_start_seconds": merged.get("block_start_seconds"),
        "block_end_seconds": merged.get("block_end_seconds"),
        "block_durations_seconds": merged.get("block_durations_seconds"),
        "stt_complete": stt_client is not None,
        "stt_model": stt_client.model if stt_client is not None else None,
        "block_count": len(blocks),
        "blocks": blocks,
        "credit_cost": sum(credits) if all(value is not None for value in credits) else None,
        "director_model": director_model if voice_provider == "fishaudio" else None,
    }
    state["status"] = "completed"
    state["audio"] = artifact
    _atomic_json(state_path, state)
    _atomic_json(output / "audio" / "latest.json", artifact)
    return artifact
