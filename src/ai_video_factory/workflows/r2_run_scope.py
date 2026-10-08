"""Persistent run identity for isolated R2 video artifacts.

Legacy LTX manifests predate this marker and retain their original GPU keys.
Never silently rewrite a paid/replayable legacy job request.
"""
from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

_SCOPE_RE = re.compile(r"^[a-f0-9]{24}$")


def video_run_scope(output: Path, run_dir: Path) -> str | None:
    """Return an isolated run ID, or None for an existing legacy LTX manifest."""
    marker = output / "video" / "r2_run_scope.txt"
    if marker.is_symlink() or marker.parent.is_symlink():
        raise ValueError("Refusing linked video R2 scope marker")
    if marker.is_file():
        token = marker.read_text(encoding="ascii").strip()
        if not _SCOPE_RE.fullmatch(token):
            raise ValueError("Invalid saved video R2 scope marker")
        return token
    # Preserve request fingerprints and R2 references on older paid runs.
    if (run_dir / "manifest.json").exists():
        return None
    marker.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex[:24]
    try:
        with marker.open("x", encoding="ascii") as stream:
            stream.write(token + "\n")
    except FileExistsError:
        token = marker.read_text(encoding="ascii").strip()
        if not _SCOPE_RE.fullmatch(token):
            raise ValueError("Invalid concurrently saved video R2 scope marker")
    return token
