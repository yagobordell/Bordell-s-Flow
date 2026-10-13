"""Prepare an immutable, per-avatar casero ebook CTA without repeating paid requests.

The first casero script chooses the copy. Later scripts reuse precisely those
assets; insertion position remains independent and is recalculated per script.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import subprocess
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from ai_video_factory.bots.cta import CTAEbook
from ai_video_factory.bots.cta_motion_copy import CTAMotionCopy
from ai_video_factory.compositor.cta_motion import build_motion
from ai_video_factory.config import settings
from ai_video_factory.providers.ai33_speech import (
    AI33SpeechClient,
    audio_url_of,
    task_id_of,
)
from ai_video_factory.providers.avatar_cta_motion import motion_source, probe_manual_motion
from ai_video_factory.providers.avatar_voice import valid_voice_provider
from ai_video_factory.providers.ebook_covers import EbookCovers
from ai_video_factory.providers.openai_images import OpenAIImageClient
from ai_video_factory.providers.openai_stt import OpenAISttClient

_PROMPT_ONE = (
    "Imagen del avatar en el mismo setup y posicion, expresion facial contento "
    "y con el libro de la primera imagen de la mano, realizando la accion de "
    "promocion del libro"
)
_PROMPT_TWO = (
    "Imagen del avatar en el mismo setup y posicion, expresion facial contento "
    "y los libros de la primera y segunda imagen de la mano, realizando la "
    "accion de promocion del libro"
)
_API_SUFFIX = (
    "\n\nAPI transport: use Responses API Structured Outputs. Return the "
    "JSON object directly matching the supplied output schema."
)


class CaseroCTAError(RuntimeError):
    """A CTA asset is unavailable or an uncertain paid operation needs reconciliation."""


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _save(path: Path, data: dict[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise CaseroCTAError(f"Unsafe CTA state path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    if tmp.exists() or tmp.is_symlink():
        raise CaseroCTAError(f"CTA state may be in flight: {tmp}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CaseroCTAError(f"Missing or unsafe CTA state: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CaseroCTAError(f"Malformed CTA state: {path}")
    return data


def _require(directory: Path, state: dict[str, Any], field: str) -> Path:
    meta = state.get(field)
    if not isinstance(meta, dict) or not isinstance(meta.get("file"), str):
        raise CaseroCTAError(f"Missing CTA asset metadata: {field}")
    relative = Path(meta["file"])
    if relative.is_absolute() or len(relative.parts) != 1:
        raise CaseroCTAError(f"Unsafe CTA asset filename: {relative}")
    file = directory / relative
    if file.is_symlink() or not file.is_file() or _sha(file) != meta.get("sha256"):
        raise CaseroCTAError(f"Missing or modified CTA asset: {file}")
    return file


def _png_data_url(file: Path) -> str:
    with Image.open(file) as image:
        if image.format != "PNG":
            raise CaseroCTAError(f"CTA reference is not PNG: {file}")
        image.verify()
    return "data:image/png;base64," + base64.b64encode(file.read_bytes()).decode("ascii")


def _convert_audio(source: Path, destination: Path) -> None:
    if shutil.which("ffmpeg") is None:
        raise CaseroCTAError("CTA TTS requires ffmpeg to create canonical PCM")
    tmp = destination.with_suffix(".part.wav")
    if destination.is_symlink() or tmp.is_symlink():
        raise CaseroCTAError("Unsafe CTA audio output")
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le",
        str(tmp),
    ], capture_output=True, text=True)
    if result.returncode:
        raise CaseroCTAError("CTA audio conversion failed: " + result.stderr[-700:])
    tmp.replace(destination)


def split_cta_audio(master: Path, first_half: Path) -> float:
    """Split at precisely half of PCM samples; pad one silence sample if odd."""
    with wave.open(str(master), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (48000, 1, 2):
            raise CaseroCTAError("CTA master must be 48-kHz mono 16-bit PCM")
        raw = source.readframes(source.getnframes())
    if len(raw) % 2:
        raise CaseroCTAError("CTA audio has an incomplete PCM sample")
    frames = len(raw) // 2
    if frames % 2:
        raw += b"\x00\x00"
        with wave.open(str(master), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            writer.writeframes(raw)
        frames += 1
    half = frames // 2
    with wave.open(str(first_half), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(raw[:half * 2])
    return half / 48000


def _tts(
    directory: Path, script: str, voice_id: str, voice_provider: str = "fishaudio",
) -> dict[str, Any]:
    state_path = directory / "tts_state.json"
    speech = AI33SpeechClient(settings.ai33_api_key or "")
    state = _read(state_path) if state_path.is_file() else {
        "status": "new", "text_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "voice_id": voice_id,
        "voice_provider": voice_provider,
    }
    if (
        state.get("text_sha256") != hashlib.sha256(script.encode()).hexdigest()
        or state.get("voice_id") != voice_id
        or state.get("voice_provider", "fishaudio") != voice_provider
    ):
        raise CaseroCTAError(
            "Saved CTA TTS belongs to a different script or voice"
        )
    if "master" in state:
        _require(directory, state, "master")
        _require(directory, state, "first_half")
        return state
    if state.get("status") == "submitting_unknown":
        raise CaseroCTAError("CTA paid TTS submission outcome unknown; reconcile tts_state.json")
    if not state.get("task_id"):
        state["status"] = "submitting_unknown"
        _save(state_path, state)
        response = speech.create(text=script, voice_id=voice_id, speed=settings.ai33_fish_speed)
        task_id = task_id_of(response)
        if not task_id:
            raise CaseroCTAError("CTA TTS submitted but no task_id was returned; inspect state")
        state.update(status="submitted", task_id=task_id)
        _save(state_path, state)
    result = speech.poll(
        state["task_id"],
        timeout_seconds=settings.ai33_fish_poll_timeout_seconds,
        interval_seconds=settings.ai33_fish_poll_interval_seconds,
    )
    url = audio_url_of(result)
    if not url:
        raise CaseroCTAError("CTA TTS task completed without usable audio URL")
    download = speech.download_audio(url, directory)
    _convert_audio(directory / download["file"], directory / "cta.wav")
    first = directory / "first_half.wav"
    half = split_cta_audio(directory / "cta.wav", first)
    state.update(
        status="completed",
        master={"file": "cta.wav", "sha256": _sha(directory/"cta.wav")},
        first_half={"file": "first_half.wav", "sha256": _sha(first)},
        half_seconds=half, duration_seconds=2*half,
    )
    _save(state_path, state)
    return state


def _stt(directory: Path) -> None:
    state_path = directory / "stt_state.json"
    if state_path.is_file():
        state = _read(state_path)
        if state.get("status") == "completed":
            _require(directory, state, "transcript")
            return
        if state.get("status") == "submitting_unknown":
            raise CaseroCTAError("CTA STT submission may have been billed; inspect stt_state.json")
    _save(state_path, {"status": "submitting_unknown", "master_sha256": _sha(directory/"cta.wav")})
    result = OpenAISttClient(api_key=settings.openai_api_key).transcribe(directory/"cta.wav")
    _save(directory/"stt.json", result)
    _save(state_path, {
        "status": "completed", "master_sha256": _sha(directory/"cta.wav"),
        "transcript": {"file": "stt.json", "sha256": _sha(directory/"stt.json")},
    })


def _image(directory: Path, covers: EbookCovers, avatar: Path,
           on_image_usage: Callable[[dict[str, object] | None, str], None] | None = None) -> None:
    image = directory / "promotion.png"
    state_path = directory / "image_state.json"
    if state_path.is_file():
        record = _read(state_path)
        if record.get("status") != "completed":
            raise CaseroCTAError(
                "CTA image submission may have been billed; reconcile image_state.json"
            )
        _require(directory, record, "image")
        return
    refs = [_png_data_url(f) for f in covers.files]
    refs.append(_png_data_url(avatar))  # contract: covers first, selected avatar LAST
    _save(state_path, {"status": "request_started_unknown", "reference_sha256": [
        *covers.sha256, _sha(avatar),
    ], "model": settings.openai_image_model, "quality": "medium"})
    result = OpenAIImageClient(settings.openai_api_key or "").generate_png(
        prompt=_PROMPT_ONE if len(covers.files) == 1 else _PROMPT_TWO,
        model=settings.openai_image_model, size="1280x720", quality="medium",
        destination=image, reference_image_urls=refs,
    )
    if on_image_usage is not None:
        on_image_usage(result.get("usage"), result["model"])
    _save(state_path, {
        "status": "completed", "reference_sha256": [*covers.sha256, _sha(avatar)],
        "image": {"file": "promotion.png", "sha256": _sha(image)},
        "usage": result.get("usage"), "model": result["model"], "quality": "medium",
    })


def _motion(directory: Path, cover: Path, qr_path: Path, qr_url: str,
            half: float, copy: CTAMotionCopy) -> None:
    """Versioned local render: never overwrite legacy completed media."""
    state_path = directory / "motion_v2_state.json"
    copy_sha = _sha(directory / "motion_copy.json")
    if state_path.is_file():
        record = _read(state_path)
        if record.get("status") != "completed":
            raise CaseroCTAError("Unreconciled CTA motion v2 rendering")
        _require(directory, record, "motion")
        if (abs(record["duration_seconds"] - (half + 1.5)) > 1e-7
                or record.get("motion_copy_sha256") != copy_sha):
            raise CaseroCTAError("Saved CTA motion v2 inputs differ")
        return
    path = directory / "motion_v2.mp4"
    if path.exists() or path.is_symlink():
        raise CaseroCTAError("CTA motion v2 exists without a verified checkpoint")
    build_motion(cover, qr_path, qr_url, half + 1.5, path, copy=copy)
    _save(state_path, {
        "status": "completed", "duration_seconds": half + 1.5,
        "motion_copy_sha256": copy_sha,
        "motion": {"file": path.name, "sha256": _sha(path)},
    })


def _adopt_manual_motion(directory: Path, half: float) -> None:
    """Record verified source once TTS fixes the required duration."""
    source = motion_source(directory)
    if source["mode"] != "manual":
        raise CaseroCTAError("Manual CTA motion source is not configured")
    path = directory / "motion_manual.mp4"
    duration = probe_manual_motion(path)
    needed = half + 1.5
    if duration + 0.015 < needed:
        raise CaseroCTAError(
            f"El motion_manual.mp4 dura {duration:.2f}s; necesita al menos "
            f"{needed:.2f}s (mitad CTA + 1.5s)."
        )
    checkpoint = directory / "motion_manual_state.json"
    expected = {"status": "completed", "duration_seconds": needed,
                "motion": {"file": path.name, "sha256": _sha(path)}}
    if checkpoint.is_file():
        if _read(checkpoint) != expected:
            raise CaseroCTAError("El motion manual difiere de su checkpoint")
        return
    _save(checkpoint, expected)


async def _motion_text(
    directory: Path, spoken_cta: str, cover: Path, provider: Any, model: str,
    on_api_response: Callable[[str, int | None, object], None] | None,
) -> CTAMotionCopy:
    destination = directory / "motion_copy.json"
    if destination.is_file():
        return CTAMotionCopy.model_validate(_read(destination))
    state = directory / "motion_copy_state.json"
    if state.is_file():
        raise CaseroCTAError(
            "CTA motion text generation previously started; reconcile motion_copy_state.json"
        )
    _save(state, {
        "status": "request_started_unknown",
        "spoken_cta_sha256": hashlib.sha256(spoken_cta.encode()).hexdigest(),
        "cover_sha256": _sha(cover),
    })
    from importlib.resources import files
    instructions = files("ai_video_factory.bots.prompts").joinpath(
        "cta_motion.md"
    ).read_text(encoding="utf-8") + _API_SUFFIX
    kwargs = {
        "model": model, "instructions": instructions,
        "input_text": json.dumps({"spoken_cta": spoken_cta}, ensure_ascii=False),
        "input_image_url": _png_data_url(cover), "output_type": CTAMotionCopy,
    }
    metered = getattr(provider, "generate_structured_with_response", None)
    if callable(metered):
        result, response = await metered(**kwargs)
        if on_api_response is not None:
            on_api_response("CTA Motion", None, response)
    else:
        result = await provider.generate_structured(**kwargs)
    if not isinstance(result, CTAMotionCopy):
        result = CTAMotionCopy.model_validate(result)
    _save(destination, result.model_dump())
    _save(state, {
        "status": "completed",
        "spoken_cta_sha256": hashlib.sha256(spoken_cta.encode()).hexdigest(),
        "cover_sha256": _sha(cover),
        "output_sha256": _sha(destination),
    })
    return result


async def _ebook_text(
    directory: Path, script: str, cover: Path, provider: Any, model: str,
    on_api_response: Callable[[str, int | None, object], None] | None,
) -> str:
    destination = directory / "script.json"
    if destination.is_file():
        return CTAEbook.model_validate(_read(destination)).cta
    state = directory / "ebook_state.json"
    if state.is_file():
        raise CaseroCTAError("CTA ebook generation previously started; reconcile ebook_state.json")
    _save(state, {"status": "request_started_unknown",
                  "first_script_sha256": hashlib.sha256(script.encode()).hexdigest()})
    from importlib.resources import files
    instructions = files("ai_video_factory.bots.prompts").joinpath(
        "cta_ebook.md"
    ).read_text(encoding="utf-8") + _API_SUFFIX
    kwargs = {
        "model": model, "instructions": instructions,
        "input_text": json.dumps({"raw_script": script}, ensure_ascii=False),
        "input_image_url": _png_data_url(cover), "output_type": CTAEbook,
    }
    metered = getattr(provider, "generate_structured_with_response", None)
    if callable(metered):
        result, response = await metered(**kwargs)
        if on_api_response is not None:
            on_api_response("CTA Ebook", None, response)
    else:
        result = await provider.generate_structured(**kwargs)
    if not isinstance(result, CTAEbook):
        result = CTAEbook.model_validate(result)
    _save(destination, result.model_dump())
    _save(state, {"status": "completed",
                  "first_script_sha256": hashlib.sha256(script.encode()).hexdigest()})
    return result.cta


async def prepare_casero_cta(
    *, avatar: Path, covers: EbookCovers, qr_path: Path, qr_url: str,
    script: str, voice_id: str, provider: Any, model: str,
    voice_provider: str = "fishaudio",
    on_api_response: Callable[[str, int | None, object], None] | None = None,
    on_call_duration: Callable[[str, int | None, float], None] | None = None,
    on_image_usage: Callable[[dict[str, object] | None, str], None] | None = None,
) -> dict[str, Any]:
    """Prepare independent assets concurrently; finished A2V is queued with main video."""
    if not script.strip() or not voice_id:
        raise CaseroCTAError("CTA requires original first script and a selected voice")
    valid_voice_provider(voice_provider)
    directory = avatar.parent / "meta" / "cta"
    if directory.is_symlink() or directory.parent.is_symlink():
        raise CaseroCTAError("Unsafe CTA cache directory")
    directory.mkdir(parents=True, exist_ok=True)
    configured_motion = motion_source(directory)
    manual_motion = configured_motion["mode"] == "manual"
    identity_path = directory / "identity.json"
    identity = {
        "schema_version": "casero-cta-v1", "avatar_sha256": _sha(avatar),
        "cover_sha256": list(covers.sha256), "qr_sha256": _sha(qr_path),
        "voice_id": voice_id, "voice_provider": voice_provider,
        "qr_url": qr_url,
    }
    if manual_motion:
        identity["manual_motion_sha256"] = configured_motion["sha256"]
    if identity_path.exists():
        saved_identity = _read(identity_path)
        if {**saved_identity, "voice_provider": saved_identity.get(
            "voice_provider", "fishaudio"
        )} != identity:
            raise CaseroCTAError(
                "Avatar CTA cache inputs have changed; manual reconciliation required"
            )
    else:
        _save(identity_path, identity)
    cached_final = (directory / "completed.json").is_file()
    if cached_final:
        ready = _read(directory / "completed.json")
        _require(directory, ready, "final")
        if ready.get("schema_version") == "casero-cta-final-v3":
            # Current Full-HD/run-v2 CTA: no paid text, speech, image or GPU calls.
            from ai_video_factory.workflows.casero_cta_video import (
                cta_assets,
                current_cta_ready,
            )
            if not current_cta_ready(directory):
                raise CaseroCTAError("Stored Full-HD CTA failed current run validation")
            cta_assets(directory)
            return {**ready, "directory": str(directory)}
        if ready.get("schema_version") == "casero-cta-final-v2":
            # Legacy motion-copy CTA remains a valid source cache. B2 video
            # orchestration will replace its 720p A2V/final clip with run-v2.
            from ai_video_factory.workflows.casero_cta_video import cta_assets
            cta_assets(directory)
            return {**ready, "directory": str(directory)}
        if ready.get("schema_version") != "casero-cta-final-v1":
            raise CaseroCTAError("Unknown finished CTA schema")
        # Before billing even the new text bot, ensure migration can run locally.
        # A v1 clip must not be silently replaced or reconstructed by a paid job.
        if not (directory / "script.json").is_file():
            raise CaseroCTAError("Legacy CTA speech script missing; keep original CTA")
        original_a2v = directory / "avatar_a2v.mp4"
        state_path = directory / "a2v_state.json"
        if not state_path.is_file():
            raise CaseroCTAError("Legacy CTA A2V checkpoint missing; keep original CTA")
        saved_a2v = _read(state_path)
        if (saved_a2v.get("status") != "completed"
                or original_a2v.is_symlink() or not original_a2v.is_file()
                or _sha(original_a2v) != saved_a2v.get("sha256")):
            raise CaseroCTAError(
                "Legacy CTA A2V cannot be verified locally; "
                "reconcile before requesting new motion copy"
            )

    from time import perf_counter
    cached_ebook_script = (directory / "script.json").is_file()
    started = perf_counter()
    text = await _ebook_text(
        directory, script, covers.files[0], provider, model, on_api_response,
    )
    if not cached_ebook_script and on_call_duration is not None:
        on_call_duration("CTA Ebook", None, perf_counter()-started)

    cached_motion_text = (directory / "motion_copy.json").is_file()
    motion_started = perf_counter()
    motion_copy_task = None if manual_motion else asyncio.create_task(_motion_text(
        directory, text, covers.files[0], provider, model, on_api_response,
    ))
    if cached_final:
        # Safe migration: only new bot+local render. Existing TTS, edit, STT
        # and A2V are never submitted again.
        tts = _read(directory / "tts_state.json")
        _require(directory, tts, "master")
        _require(directory, tts, "first_half")
        if manual_motion:
            await asyncio.to_thread(_adopt_manual_motion, directory, float(tts["half_seconds"]))
        else:
            if motion_copy_task is None:
                raise CaseroCTAError("Automatic motion task was not prepared")
            copy = await asyncio.shield(motion_copy_task)
            if not cached_motion_text and on_call_duration is not None:
                on_call_duration("CTA Motion", None, perf_counter() - motion_started)
            await asyncio.to_thread(
                _motion, directory, covers.files[0], qr_path, qr_url,
                float(tts["half_seconds"]), copy,
            )
        from ai_video_factory.workflows.casero_cta_video import upgrade_cached_cta
        upgraded = await asyncio.to_thread(upgrade_cached_cta, directory)
        return {"status": "completed", "directory": str(directory),
                "final": {"file": upgraded.name, "sha256": _sha(upgraded)}}

    # New avatar CTA: prepare the bot while speech and image run independently.
    # Wait for *every* paid call even when a sibling task fails. Ordinary
    # gather can raise early while a billed TTS/image request still runs.
    tts_task = asyncio.create_task(asyncio.to_thread(
        _tts, directory, text, voice_id, voice_provider,
    ))
    image_task = asyncio.create_task(asyncio.to_thread(
        _image, directory, covers, avatar, on_image_usage,
    ))
    tasks = [tts_task, image_task]
    if motion_copy_task is not None:
        tasks.append(motion_copy_task)
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    tts = outcomes[0]
    if not manual_motion and not cached_motion_text and on_call_duration is not None:
        on_call_duration("CTA Motion", None, perf_counter() - motion_started)
    motion_job = (
        asyncio.to_thread(_adopt_manual_motion, directory, float(tts["half_seconds"]))
        if manual_motion else
        asyncio.to_thread(
            _motion, directory, covers.files[0], qr_path, qr_url,
            float(tts["half_seconds"]), outcomes[2],
        )
    )
    finishing = await asyncio.gather(
        asyncio.to_thread(_stt, directory), motion_job,
        return_exceptions=True,
    )
    for outcome in finishing:
        if isinstance(outcome, BaseException):
            raise outcome
    return {
        **identity, "status": "assets_ready", "directory": str(directory),
        "half_seconds": tts["half_seconds"], "duration_seconds": tts["duration_seconds"],
    }
