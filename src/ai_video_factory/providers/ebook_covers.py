"""One-time, immutable ebook cover selection for a casero avatar."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

_STEM = re.compile(r"[\w .()-]{1,100}\Z", re.UNICODE)


@dataclass(frozen=True)
class EbookCovers:
    files: tuple[Path, ...]
    sha256: tuple[str, ...]


def _safe_cover(root: Path, stem: str) -> Path:
    if (not isinstance(stem, str) or not _STEM.fullmatch(stem)
            or stem in {".", ".."} or stem.endswith(".png")):
        raise ValueError("Indica solo el nombre del PNG, sin .png ni carpetas.")
    root = root.resolve(strict=True)
    path = root / (stem + ".png")
    if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
        raise ValueError(f"Portada no encontrada en {root}: {stem}.png")
    with Image.open(path) as img:
        if img.format != "PNG":
            raise ValueError(f"La portada no es PNG: {path}")
        img.verify()
    return path


def ensure_ebook_covers(
    avatar: Path, *, data_dir: Path = Path("data"),
    names: Sequence[str] | None = None, interactive: bool | None = None,
) -> EbookCovers:
    """Persist principal then optional secondary by SHA; never silently replace."""
    meta = avatar.parent / "meta"
    if meta.is_symlink() or avatar.is_symlink():
        raise ValueError("No se permiten enlaces para portadas de CTA.")
    meta.mkdir(parents=True, exist_ok=True)
    record_file = meta / "ebook_covers.json"
    if record_file.is_symlink():
        raise ValueError("No se permiten enlaces en ebook_covers.json")
    record = None
    if record_file.is_file():
        record = json.loads(record_file.read_text(encoding="utf-8"))
        if record.get("avatar") != avatar.name or len(record.get("covers", [])) not in (1, 2):
            raise ValueError("Configuración de portadas inválida.")
        saved = tuple(item["name"] for item in record["covers"])
        if names is not None and tuple(names) != saved:
            raise ValueError(
                "Las portadas del avatar ya están fijadas; no se cambiarán automáticamente."
            )
        selected = saved
    else:
        if names is None:
            if interactive is None:
                interactive = sys.stdin.isatty()
            if not interactive:
                raise ValueError(
                    "Casero requiere --ebook-cover NOMBRE (una o dos veces) en el primer run."
                )
            first = input("Portada principal (nombre sin .png, en data/): ").strip()
            second = input("Portada secundaria opcional (Enter para ninguna, sin .png): ").strip()
            selected = (first, second) if second else (first,)
        else:
            selected = tuple(names)
        if len(selected) not in (1, 2) or len(set(selected)) != len(selected):
            raise ValueError("Indica una o dos portadas PNG distintas, en orden de prioridad.")
    # Snapshot the first selection so a future generation can reuse the avatar
    # even after the original data/ uploads have been moved or removed.
    snapshot_dir = meta / "ebook"
    if snapshot_dir.is_symlink():
        raise ValueError("La carpeta de portadas del avatar no puede ser un enlace.")
    source_files: list[Path] = []
    hashes: list[str] = []
    for number, item in enumerate(selected, 1):
        pinned = snapshot_dir / f"cover_{number}.png"
        original = data_dir / (item + ".png")
        if original.is_file():
            original = _safe_cover(data_dir, item)
            digest = hashlib.sha256(original.read_bytes()).hexdigest()
            if record is not None and digest != record["covers"][number-1]["sha256"]:
                raise ValueError(
                    "La portada original ha cambiado respecto a la selección guardada."
                )
            if pinned.is_file() and (
                pinned.is_symlink() or hashlib.sha256(pinned.read_bytes()).hexdigest() != digest
            ):
                raise ValueError("La copia persistente de la portada ha cambiado.")
            if not pinned.is_file():
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                temporary_cover = pinned.with_suffix(".part")
                if temporary_cover.exists() or temporary_cover.is_symlink():
                    raise ValueError("Copia de portada incompleta; revisa .part")
                try:
                    temporary_cover.write_bytes(original.read_bytes())
                    temporary_cover.replace(pinned)
                finally:
                    temporary_cover.unlink(missing_ok=True)
        else:
            if record is None or pinned.is_symlink() or not pinned.is_file():
                raise ValueError(f"Falta la portada original y su copia: {item}.png")
            digest = hashlib.sha256(pinned.read_bytes()).hexdigest()
            if digest != record["covers"][number-1]["sha256"]:
                raise ValueError("La portada guardada ya no coincide con su SHA-256.")
        source_files.append(pinned)
        hashes.append(digest)
    if record is not None:
        if tuple(item["sha256"] for item in record["covers"]) != tuple(hashes):
            raise ValueError("Ha cambiado una portada vinculada a este avatar.")
    else:
        tmp = record_file.with_suffix(".json.part")
        if tmp.exists() or tmp.is_symlink():
            raise ValueError(
                "Selección anterior de portadas interrumpida; revisa el archivo .part."
            )
        try:
            tmp.write_text(json.dumps({
                "schema_version": "ebook-covers-v1", "avatar": avatar.name,
                "covers": [{"name": s, "sha256": h} for s, h in zip(selected, hashes, strict=True)],
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(record_file)
        finally:
            tmp.unlink(missing_ok=True)
    return EbookCovers(tuple(source_files), tuple(hashes))


def resolve_professional_ebook_covers(
    avatar: Path, *, script: str, data_dir: Path,
    names: Sequence[str] | None = None,
    interactive: bool | None = None,
) -> EbookCovers | None:
    """Ask for the real PNG once when Professional narration promotes an ebook.

    Preserve Casero's existing optional-two-cover selection unchanged.
    A real spoken QR/ebook CTA is a reason to ask for its asset, not a reason
    to raise after preparing a run or to delete the promotion from the plan.
    """
    from ai_video_factory.agents.professional.preflight import requires_ebook_cover

    record = avatar.parent / "meta" / "ebook_covers.json"
    required = requires_ebook_cover(script)
    if names is None and not required and not record.is_file() and not record.is_symlink():
        return None
    selected = names
    if selected is None and required and not record.is_file():
        if record.is_symlink():
            raise ValueError("No se permiten enlaces en ebook_covers.json.")
        if interactive is None:
            interactive = sys.stdin.isatty()
        if not interactive:
            raise ValueError(
                "El guion promociona un ebook y necesita su portada PNG. "
                "Indica --ebook-cover NOMBRE (archivo data/NOMBRE.png) "
                "en esta ejecución no interactiva."
            )
        print(
            "\n  [Profesional] El guion incluye un CTA de ebook con QR. "
            "Selecciona su portada auténtica antes de planificar el vídeo.",
            flush=True,
        )
        try:
            cover_name = input(
                "  Nombre de la portada PNG en data/ (con o sin .png): "
            ).strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise ValueError("Selección de portada cancelada.") from exc
        if cover_name.lower().endswith(".png"):
            cover_name = cover_name[:-4]
        if not cover_name:
            raise ValueError("Es necesario indicar el nombre de la portada PNG.")
        selected = (cover_name,)
    return ensure_ebook_covers(
        avatar, data_dir=data_dir, names=selected, interactive=interactive,
    )
