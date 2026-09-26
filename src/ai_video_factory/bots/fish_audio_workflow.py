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
from ai_video_factory.providers.base import StructuredTextProvider

from .audio_join import join_audio_blocks
from .contracts import B11Output, B12Input
from .fish_audio import (
    FishAudioScriptError,
    fish_prompt_bytes,
    run_fish_director,
    validate_fish_script,
)
from .workflow import materialize_blocks

_DEFAULT_VOICE_ID = "fishaudio_80e34d5e0b2b4577a486f3a77e357261"


class FishAudioWorkflowError(RuntimeError):
    """Speech state requires recovery or its saved source no longer matches."""


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
            "speed": speed,
            "status": "director_running",
        }
        _atomic_json(directory / "input.json", input_data)
        _atomic_json(state_path, state)

    if (directory / "output.json").exists():
        directed = _read_json(directory / "output.json")
        directed_text = directed.get("plain_script_for_recording")
        if not isinstance(directed_text, str):
            raise FishAudioWorkflowError(f"Malformed saved Fish output for block {block.block_id}")
        validate_fish_script(block.text, directed_text)
    else:
        if state.get("task_id") or state.get("status") == "tts_submitting_unknown":
            raise FishAudioWorkflowError("Paid Fish task has no durable director output")
        if provider is None:
            raise FishAudioWorkflowError(
                f"Block {block.block_id} has no completed director; "
                "resume requires a validated saved output"
            )
        state["status"] = "director_running"
        _atomic_json(state_path, state)
        try:
            directed, response = await run_fish_director(
                payload, provider=provider, model=director_model
            )
        except FishAudioScriptError:
            state["status"] = "director_invalid"
            _atomic_json(state_path, state)
            raise
        except Exception:
            state["status"] = "director_unknown"
            _atomic_json(state_path, state)
            raise
        directed_text = directed.plain_script_for_recording
        _atomic_json(directory / "output.json", directed.model_dump())
        usage = getattr(response, "usage", None)
        if usage is not None and callable(getattr(usage, "model_dump", None)):
            state["director_usage"] = usage.model_dump(mode="json")
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
    resume: bool = False,
    speech_client: AI33SpeechClient | None = None,
) -> dict[str, Any]:
    """Run each block independently, then publish a single ordered WAV narration."""
    if not isinstance(script, str) or not script.strip():
        raise FishAudioWorkflowError("The source script must not be empty")
    if not voice_id.startswith("fishaudio_"):
        raise FishAudioWorkflowError("Fish voice ID is not a Fish Audio source ID")
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
            or state.get("director_model") != director_model
            or state.get("director_prompt_sha256")
            != hashlib.sha256(fish_prompt_bytes()).hexdigest()
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
            "director_prompt_sha256": hashlib.sha256(fish_prompt_bytes()).hexdigest(),
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
    if provider is None and not resume:
        raise FishAudioWorkflowError("OPENAI_API_KEY is required for the Fish director")

    semaphore = asyncio.Semaphore(max_parallel_calls)

    async def one(payload: B12Input) -> dict[str, Any]:
        async with semaphore:
            return await _generate_block(
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
            )

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
        "voice_source": "fishaudio",
        "voice_id": voice_id,
        "run_id": state["run_id"],
        "file": (run_dir / merged["file"]).relative_to(output).as_posix(),
        "sha256": merged["sha256"],
        "format": merged["format"],
        "bytes": merged["bytes"],
        "duration_seconds": merged["duration_seconds"],
        "pause_between_blocks_seconds": 0.5,
        "block_count": len(blocks),
        "blocks": blocks,
        "credit_cost": sum(credits) if all(value is not None for value in credits) else None,
        "director_model": director_model,
    }
    state["status"] = "completed"
    state["audio"] = artifact
    _atomic_json(state_path, state)
    _atomic_json(output / "audio" / "latest.json", artifact)
    return artifact
