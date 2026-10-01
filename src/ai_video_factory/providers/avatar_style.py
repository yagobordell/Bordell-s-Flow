"""Persistent professional/casero visual style, chosen exactly once per managed avatar."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

VisualStyle = Literal["casero", "profesional"]


def ensure_avatar_style(
    avatar_file: Path,
    requested: str | None,
    choose: Callable[[str | None], str],
) -> VisualStyle:
    """Return the fixed style. Never prompt for a correctly saved style.

    An explicit incompatible --style is rejected, not used to silently change
    the avatar's identity or rerun a checkpoint with a different B2 policy.
    """
    metadata = avatar_file.parent / "meta"
    if metadata.is_symlink() or metadata.is_junction() or not metadata.is_dir():
        raise SystemExit(f"Falta la carpeta meta del avatar: {metadata}")
    record_path = metadata / "style.json"
    temporary = metadata / "style.json.part"
    if record_path.is_symlink() or temporary.is_symlink() or temporary.is_junction():
        raise SystemExit("No se permiten enlaces en los metadatos del estilo.")
    if temporary.exists():
        raise SystemExit(
            f"Hay una selección de estilo incompleta: {temporary}. Revísala antes de seguir."
        )
    if requested is not None and requested not in {"casero", "profesional"}:
        raise SystemExit(f"Estilo no válido: {requested}")
    if record_path.exists():
        if not record_path.is_file():
            raise SystemExit(f"Metadatos de estilo inválidos: {record_path}")
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SystemExit(f"Metadatos de estilo dañados: {record_path}") from exc
        if (
            not isinstance(record, dict)
            or record.get("avatar_filename") != avatar_file.name
            or record.get("visual_style") not in {"casero", "profesional"}
        ):
            raise SystemExit(f"Metadatos de estilo inválidos: {record_path}")
        stored = record["visual_style"]
        if requested is not None and requested != stored:
            raise SystemExit(
                f"El avatar tiene fijado el estilo {stored}. "
                f"Revisa {record_path} antes de cambiarlo."
            )
        return stored

    style = choose(requested)
    if style not in {"casero", "profesional"}:
        raise SystemExit(f"El selector devolvió un estilo no válido: {style}")
    payload = {
        "avatar_filename": avatar_file.name,
        "visual_style": style,
    }
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, ensure_ascii=False)
            output.write("\n")
        if record_path.exists() or record_path.is_symlink():
            raise SystemExit(f"Ya existe un estilo para este avatar: {record_path}")
        temporary.replace(record_path)
    finally:
        temporary.unlink(missing_ok=True)
    return style
