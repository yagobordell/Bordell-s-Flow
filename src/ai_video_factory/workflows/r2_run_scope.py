"""Persistent run identity for isolated R2 video artifacts.

Legacy LTX manifests predate this marker and retain their original GPU keys.
Never silently rewrite a paid/replayable legacy job request.
"""
from __future__ import annotations

import re
from contextvars import ContextVar, Token
from pathlib import Path
from uuid import uuid4

from ai_video_factory.inference.contracts import InferenceJobRequest

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

_ACTIVE_SCOPE: ContextVar[str | None] = ContextVar("video_r2_scope", default=None)


def set_active_video_scope(scope: str | None) -> Token[str | None]:
    if scope is not None and not _SCOPE_RE.fullmatch(scope):
        raise ValueError("Invalid video R2 scope")
    return _ACTIVE_SCOPE.set(scope)


def reset_active_video_scope(token: Token[str | None]) -> None:
    _ACTIVE_SCOPE.reset(token)


def saved_video_scope(output: Path) -> str | None:
    marker = output / "video" / "r2_run_scope.txt"
    if marker.is_symlink() or marker.parent.is_symlink():
        raise ValueError("Refusing linked video R2 scope marker")
    if not marker.is_file():
        return None
    scope = marker.read_text(encoding="ascii").strip()
    if not _SCOPE_RE.fullmatch(scope):
        raise ValueError("Invalid saved video R2 scope")
    return scope


def scope_inference_request(request: InferenceJobRequest) -> InferenceJobRequest:
    """Isolate generated worker inputs/outputs, preserving immutable voice sources.

    Scope is explicit and task-local; smoke tests and legacy runs retain their
    existing immutable request IDs and global cache behavior.
    """
    scope = _ACTIVE_SCOPE.get()
    if scope is None or request.job_id.endswith(f"-r{scope}"):
        return request
    job_id = f"{request.job_id}-r{scope}"
    prefix = f"jobs/{job_id}"
    inputs = [
        item if item.name == "reference_audio" else item.model_copy(
            update={"key": f"{prefix}/inputs/{item.name}{Path(item.key).suffix}"}
        )
        for item in request.inputs
    ]
    output = request.output.model_copy(
        update={"key": f"{prefix}/{Path(request.output.key).name}"}
    )
    sidecars = {
        name: contract.model_copy(
            update={"key": f"{prefix}/{Path(contract.key).name}"}
        )
        for name, contract in request.sidecar_outputs.items()
    }
    return request.model_copy(update={
        "job_id": job_id,
        "inputs": inputs,
        "output": output,
        "sidecar_outputs": sidecars,
    })
