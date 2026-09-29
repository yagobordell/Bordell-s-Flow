"""Keep each avatar PNG, QR and voice configuration in one local directory."""

from __future__ import annotations

import json
from pathlib import Path

_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm", ".flac", ".opus"}
_LEGACY_FILES = {
    ".qr": {"qr.png", "qr.json", "request_pending.json"},
    ".voice": {"voice.json"},
}


def _no_link(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise SystemExit(f"No se permiten enlaces en la biblioteca de avatares: {path}")


def avatar_directory(avatar_file: Path, avatars_dir: Path) -> Path:
    """Return the canonical folder without changing an unmigrated PNG."""
    _no_link(avatars_dir)
    _no_link(avatar_file)
    _no_link(avatar_file.parent)
    if not avatar_file.is_file() or avatar_file.suffix.lower() != ".png":
        raise SystemExit(f"El avatar debe ser un PNG local: {avatar_file}")
    if avatar_file.stem in {".qr", ".voice", ".", ".."}:
        raise SystemExit(f"Nombre de avatar reservado: {avatar_file.name}")
    if avatar_file.parent.resolve() == avatars_dir.resolve():
        return avatars_dir / avatar_file.stem
    if (
        avatar_file.parent.parent.resolve() == avatars_dir.resolve()
        and avatar_file.parent.name == avatar_file.stem
    ):
        return avatar_file.parent
    raise SystemExit(
        f"El avatar debe estar en {avatars_dir} o en su carpeta con el mismo nombre: "
        f"{avatar_file}"
    )


def _recorded_audio(legacy_voice: Path) -> str | None:
    """Find a reference already bound to a legacy clone, if present."""
    if not legacy_voice.is_file():
        return None
    try:
        record = json.loads(legacy_voice.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("mode") != "clone":
        return None
    name = record.get("audio_filename")
    if (
        isinstance(name, str)
        and name not in {"", ".", ".."}
        and Path(name).name == name
        and "/" not in name
        and "\\" not in name
        and Path(name).suffix.lower() in _AUDIO_SUFFIXES
    ):
        return name
    return None


def organize_avatar(
    avatar_file: Path, avatars_dir: Path, *, reference_audio: str | None = None
) -> Path:
    """Migrate on use, without losing existing QR/voice records or overwriting files.

    Metadata is moved before the PNG. If interrupted, the next invocation can
    finish migrating the remaining files without repeating a paid clone or QR.
    """
    folder = avatar_directory(avatar_file, avatars_dir)
    _no_link(folder)
    if folder.exists() and not folder.is_dir():
        raise SystemExit(f"La carpeta del avatar no es un directorio: {folder}")
    target_png = folder / avatar_file.name
    moving_png = avatar_file.parent.resolve() == avatars_dir.resolve()
    if moving_png and (target_png.exists() or target_png.is_symlink()):
        raise SystemExit(f"Ya existe otro PNG para este avatar: {target_png}")

    moves: list[tuple[Path, Path]] = []
    old_slots: list[Path] = []
    for store_name, allowed in _LEGACY_FILES.items():
        store = avatars_dir / store_name
        old_slot = store / avatar_file.name
        _no_link(store)
        _no_link(old_slot)
        if not old_slot.exists():
            continue
        if not old_slot.is_dir():
            raise SystemExit(f"Almacén antiguo inválido: {old_slot}")
        old_slots.append(old_slot)
        for source in old_slot.iterdir():
            _no_link(source)
            if not source.is_file() or source.name not in allowed:
                raise SystemExit(
                    f"Hay un archivo antiguo sin migrar o temporal: {source}. "
                    "Revísalo antes de continuar."
                )
            moves.append((source, folder / source.name))

    audio_names: set[str] = set()
    if reference_audio is not None:
        if (
            Path(reference_audio).name != reference_audio
            or "/" in reference_audio
            or "\\" in reference_audio
            or Path(reference_audio).suffix.lower() not in _AUDIO_SUFFIXES
        ):
            raise SystemExit("El audio de referencia debe ser un nombre local válido.")
        audio_names.add(reference_audio)
    recorded = _recorded_audio(avatars_dir / ".voice" / avatar_file.name / "voice.json")
    if recorded is not None:
        audio_names.add(recorded)
    # A same-stem reference placed beside a new PNG moves with it, too.
    for path in avatars_dir.iterdir():
        if (
            path.is_file()
            and path.stem.casefold() == avatar_file.stem.casefold()
            and path.suffix.lower() in _AUDIO_SUFFIXES
        ):
            audio_names.add(path.name)
    for name in sorted(audio_names):
        source = avatars_dir / name
        _no_link(source)
        if source.is_file():
            moves.append((source, folder / name))

    if moving_png:
        moves.append((avatar_file, target_png))
    for source, target in moves:
        _no_link(target)
        if target.exists() or target.is_symlink():
            raise SystemExit(
                f"No se sobrescribirá {target}; revisa el conflicto antes de migrar."
            )
        if source == target:
            raise SystemExit(f"Origen y destino de migración coinciden: {source}")

    if not folder.exists():
        folder.mkdir()
    for source, target in moves:
        # Do not silently overwrite a file created after preflight.
        if target.exists() or target.is_symlink():
            raise SystemExit(f"Conflicto durante la migración: {target}")
        source.rename(target)
    for old_slot in old_slots:
        old_slot.rmdir()
        store = old_slot.parent
        if not any(store.iterdir()):
            store.rmdir()
    return target_png
