"""One-time, immutable ebook cover selection for a casero avatar."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image

_STEM = re.compile(r"[\w .()-]{1,100}\Z", re.UNICODE)


@dataclass(frozen=True)
class EbookCovers:
    files: tuple[Path, ...]
    sha256: tuple[str, ...]


def _safe_cover(root: Path, stem: str) -> Path:
    if not isinstance(stem, str) or not _STEM.fullmatch(stem) or stem in {".", ".."} or stem.endswith(".png"):
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
            raise ValueError("Las portadas del avatar ya están fijadas; no se cambiarán automáticamente.")
        selected = saved
    else:
        if names is None:
            if interactive is None:
                interactive = sys.stdin.isatty()
            if not interactive:
                raise ValueError("Casero requiere --ebook-cover NOMBRE (una o dos veces) en el primer run.")
            first = input("Portada principal (nombre sin .png, en data/): ").strip()
            second = input("Portada secundaria opcional (Enter para ninguna, sin .png): ").strip()
            selected = (first, second) if second else (first,)
        else:
            selected = tuple(names)
        if len(selected) not in (1, 2) or len(set(selected)) != len(selected):
            raise ValueError("Indica una o dos portadas PNG distintas, en orden de prioridad.")
    covers = tuple(_safe_cover(data_dir, item) for item in selected)
    hashes = tuple(hashlib.sha256(item.read_bytes()).hexdigest() for item in covers)
    if record is not None:
        if tuple(item["sha256"] for item in record["covers"]) != hashes:
            raise ValueError("Ha cambiado una portada ya vinculada al avatar. Revisa el CTA guardado.")
    else:
        tmp = record_file.with_suffix(".json.part")
        if tmp.exists() or tmp.is_symlink():
            raise ValueError("Selección anterior de portadas interrumpida; revisa el archivo .part.")
        try:
            tmp.write_text(json.dumps({
                "schema_version": "ebook-covers-v1", "avatar": avatar.name,
                "covers": [{"name": s, "sha256": h} for s, h in zip(selected, hashes, strict=True)],
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(record_file)
        finally:
            tmp.unlink(missing_ok=True)
    return EbookCovers(covers, hashes)
