"""Persistent per-avatar AI33 voice selection, independent of generated run folders."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_video_factory.providers.ai33_speech import (
    AI33SpeechClient,
    task_id_of,
    voice_id_of,
)

_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm", ".flac", ".opus"}
_MAX_AUDIO_BYTES = 10 * 1024 * 1024
_VOICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:-]{1,255}\Z")


@dataclass(frozen=True)
class AvatarVoice:
    mode: str
    voice_id: str | None
    audio_filename: str | None = None
    audio_sha256: str | None = None

    def snapshot(self) -> dict[str, str | None]:
        return {
            "mode": self.mode,
            "voice_id": self.voice_id,
            "audio_filename": self.audio_filename,
            "audio_sha256": self.audio_sha256,
        }


def _valid_voice_id(value: str) -> str:
    value = value.strip()
    if not _VOICE_ID.fullmatch(value):
        raise ValueError("El Voice ID de AI33Pro no tiene un formato válido.")
    return value


def _reference(avatar_file: Path, filename: str) -> tuple[str, str]:
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError("Indica solo el nombre del audio situado junto al PNG del avatar.")
    if "/" in filename or "\\" in filename or Path(filename).suffix.lower() not in _AUDIO_SUFFIXES:
        raise ValueError("Audio no admitido; utiliza MP3, WAV, M4A, AAC, OGG, WebM, FLAC u Opus.")
    path = avatar_file.parent / filename
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"El audio {filename} no existe junto al avatar {avatar_file.name}.")
    size = path.stat().st_size
    if size < 1 or size > _MAX_AUDIO_BYTES:
        raise ValueError("El audio de referencia debe ocupar entre 1 byte y 10 MiB.")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return filename, digest


def _safe_slot(avatar_file: Path, avatars_dir: Path) -> tuple[Path, Path]:
    store = avatars_dir / ".voice"
    slot = store / avatar_file.name
    record_path = slot / "voice.json"
    if avatar_file.is_symlink() or avatar_file.parent.resolve() != avatars_dir.resolve():
        raise SystemExit("El avatar debe ser un PNG local no enlazado de --avatars-dir.")
    paths = (store, slot, record_path, record_path.with_suffix(".json.part"))
    if any(path.is_symlink() for path in paths):
        raise SystemExit("No se permiten enlaces simbólicos en el almacén de voces.")
    return slot, record_path


def _save(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.part")
    if path.is_symlink() or temporary.is_symlink() or temporary.exists():
        raise SystemExit(f"Revisa el archivo temporal de voz antes de continuar: {temporary}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path: Path, avatar_file: Path) -> dict[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("avatar_filename") != avatar_file.name:
            raise ValueError("Los metadatos no corresponden al avatar.")
        mode = record.get("mode")
        status = record.get("status")
        if mode not in {"default", "voice_id", "clone"} or status not in {
            "ready", "selected", "clone_submitting_unknown", "clone_poll_pending"
        }:
            raise ValueError("Estado de voz desconocido.")
        if mode == "voice_id" or (mode == "clone" and status == "ready"):
            _valid_voice_id(record["voice_id"])
        if mode == "clone":
            filename, digest = _reference(avatar_file, record["audio_filename"])
            if digest != record["audio_sha256"]:
                raise ValueError(f"El audio {filename} cambió desde la selección inicial.")
        return record
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(
            f"Configuración de voz inválida para {avatar_file.name}: {path}: {exc}"
        ) from exc


def _choose(avatar_file: Path, *, interactive: bool) -> tuple[str, str | None]:
    if not interactive:
        return "default", None
    print(
        f"Voz para {avatar_file.name}: introduce un Voice ID de AI33Pro, "
        "el nombre de un audio de referencia junto al PNG, o q para usar la voz predeterminada.",
        flush=True,
    )
    while True:
        try:
            entered = input("Voice ID / archivo de audio / q (voz por defecto): ").strip()
        except (EOFError, KeyboardInterrupt):
            return "default", None
        if entered.lower() in {"q", "quit", "cancel", "cancelar", ""}:
            return "default", None
        if Path(entered).suffix.lower() in _AUDIO_SUFFIXES:
            try:
                _reference(avatar_file, entered)
            except ValueError as exc:
                print(exc, flush=True)
                continue
            return "clone", entered
        try:
            return "voice_id", _valid_voice_id(entered)
        except ValueError as exc:
            print(exc, flush=True)


def ensure_avatar_voice(
    *,
    avatar_file: Path,
    avatars_dir: Path,
    default_voice_id: str,
    input_voice_id: str | None = None,
    input_audio_filename: str | None = None,
    use_default: bool = False,
    interactive: bool | None = None,
    activate: bool = True,
    api_key: str | None = None,
    client: AI33SpeechClient | None = None,
) -> AvatarVoice:
    """Select once, reuse forever and never repeat an ambiguous paid clone POST.

    Noninteractive runs without a selection use the existing default voice.
    A clone selected during --no-audio/--stop is saved but submitted only when needed.
    """
    _valid_voice_id(default_voice_id)
    if sum((input_voice_id is not None, input_audio_filename is not None, use_default)) > 1:
        raise SystemExit("Usa solo una opción entre --voice-id, --voice-audio y --default-voice.")
    _slot, path = _safe_slot(avatar_file, avatars_dir)
    record = _load(path, avatar_file) if path.exists() else None

    if input_voice_id is not None:
        desired_mode, desired_value = "voice_id", _valid_voice_id(input_voice_id)
    elif input_audio_filename is not None:
        desired_mode, desired_value = "clone", _reference(avatar_file, input_audio_filename)[0]
    elif use_default:
        desired_mode, desired_value = "default", None
    else:
        desired_mode, desired_value = None, None

    if record is not None and desired_mode is not None and (
        record["mode"] != desired_mode
        or (
            desired_mode == "voice_id" and record.get("voice_id") != desired_value
        )
        or (
            desired_mode == "clone" and record.get("audio_filename") != desired_value
        )
    ):
        raise SystemExit(f"El avatar ya tiene una voz asignada. Revisa {path} antes de cambiarla.")

    if record is None:
        if desired_mode is None:
            if interactive is None:
                interactive = sys.stdin.isatty()
            desired_mode, desired_value = _choose(avatar_file, interactive=interactive)
        record = {
            "avatar_filename": avatar_file.name,
            "mode": desired_mode,
            "status": "ready" if desired_mode != "clone" else "selected",
        }
        if desired_mode == "voice_id":
            record["voice_id"] = desired_value
        elif desired_mode == "clone":
            filename, digest = _reference(avatar_file, str(desired_value))
            record["audio_filename"] = filename
            record["audio_sha256"] = digest
        _save(path, record)

    if record["mode"] == "default":
        return AvatarVoice(mode="default", voice_id=default_voice_id)
    if record["mode"] == "voice_id" or record["status"] == "ready":
        return AvatarVoice(
            mode=record["mode"],
            voice_id=record["voice_id"],
            audio_filename=record.get("audio_filename"),
            audio_sha256=record.get("audio_sha256"),
        )

    if not activate:
        return AvatarVoice(
            mode="clone", voice_id=None,
            audio_filename=record["audio_filename"],
            audio_sha256=record["audio_sha256"],
        )
    if not (api_key or client):
        raise SystemExit("AI33_API_KEY es necesaria para clonar la voz del avatar.")

    if client is None:
        client = AI33SpeechClient(str(api_key))
    if record["status"] == "clone_submitting_unknown":
        raise SystemExit(
            f"La petición de clonación pudo haber sido cobrada. Revisa {path} y "
            "recupera su Voice ID en AI33Pro; nunca se enviará de nuevo automáticamente."
        )

    if record["status"] == "selected":
        # Save BEFORE the paid POST: a network timeout must not create duplicate clones.
        record["status"] = "clone_submitting_unknown"
        _save(path, record)
        audio_path = avatar_file.parent / record["audio_filename"]
        try:
            created = client.clone_voice(audio_path=audio_path, name=avatar_file.stem)
        except Exception as exc:
            raise SystemExit(
                "No se puede confirmar si AI33Pro aceptó la clonación. "
                f"Revisa {path} y la cuenta antes de repetir la operación."
            ) from exc
        record["create_response"] = created
        direct_id = voice_id_of(created)
        if direct_id is not None:
            record["voice_id"] = direct_id
            record["status"] = "ready"
            _save(path, record)
        else:
            task_id = task_id_of(created)
            if not task_id:
                _save(path, record)
                raise SystemExit(
                    "AI33Pro no devolvió ni Voice ID ni task_id. "
                    f"Revisa {path}; no se repetirá la petición pagada."
                )
            record["task_id"] = task_id
            record["status"] = "clone_poll_pending"
            _save(path, record)

    if record["status"] == "clone_poll_pending":
        try:
            result = client.poll(record["task_id"])
        except Exception as exc:
            raise SystemExit(
                f"La clonación sigue pendiente o ha fallado. Consulta {path} "
                "y vuelve a ejecutar para consultar el task_id sin duplicar el POST."
            ) from exc
        record["poll_response"] = result
        voice_id = voice_id_of(result)
        if voice_id is None:
            _save(path, record)
            raise SystemExit(
                f"La tarea {record['task_id']} no devolvió un Voice ID. "
                f"Consulta {path} antes de generar narración."
            )
        record["voice_id"] = voice_id
        record["status"] = "ready"
        _save(path, record)

    return AvatarVoice(
        mode="clone",
        voice_id=record["voice_id"],
        audio_filename=record["audio_filename"],
        audio_sha256=record["audio_sha256"],
    )
