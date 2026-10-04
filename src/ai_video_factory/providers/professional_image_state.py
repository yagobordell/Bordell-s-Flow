"""Shared verification for professional images finished in their original beat path.

Keep paid GPT generation identities immutable even when the verified final PNG
replaces the source bytes. Only trusted, completed or recoverable finishing state
may explain a changed source file on a resumed B2 run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def finishing_state_path(output: Path, block: int, beat: str, recipe: str) -> Path:
    return (
        output / "images" / "finishing_state" / f"block_{block}"
        / f"{beat}-{recipe}.json"
    )


def finished_in_place_sha(
    output: Path, *, file: str, source_sha256: str,
    block: int, beat: str, recipes: tuple[str, ...],
) -> str | None:
    relative = Path(file)
    if (relative.is_absolute() or ".." in relative.parts
        or not relative.parts or relative.parts[0] != "images"):
        return None
    png = output / relative
    for recipe in recipes:
        state_path = finishing_state_path(output, block, beat, recipe)
        if not state_path.is_file():
            continue
        if (state_path.is_symlink() or state_path.parent.is_symlink()
            or png.is_symlink() or not png.is_file()):
            raise ValueError(f"Unsafe professional finishing state: {state_path}")
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        if (
            saved.get("schema_version") != "professional-image-finishing-v1"
            or saved.get("source") != file
            or saved.get("source_sha256") != source_sha256
            or saved.get("output_file") != file
            or saved.get("status") not in {"completed", "ready_to_replace"}
        ):
            continue
        digest = hashlib.sha256(png.read_bytes()).hexdigest()
        if digest == saved.get("sha256"):
            return digest
    return None
