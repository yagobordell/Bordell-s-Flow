"""Small shared persistence and artifact checks for Phase 8 video workflows."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal, Protocol

from ai_video_factory.inference.contracts import OutputArtifact
from ai_video_factory.inference.ports import ObjectStorage
from ai_video_factory.inference.storage import sha256_file


class TerminalJobState(Protocol):
    shot_id: int
    transport_status: str
    transport_job_id: str | None
    last_terminal_payload: dict[str, Any] | None


def archive_manifest(path: Path, run_fingerprint: str, *, phase: Literal["generation", "upscale"]) -> Path:
    raw = path.read_bytes()
    state_sha = hashlib.sha256(raw).hexdigest()[:12]
    archive = path.with_name(
        f"{path.stem}.archive-{run_fingerprint[:12]}-{state_sha}{path.suffix}"
    )
    if archive.exists():
        if archive.read_bytes() != raw:
            raise ValueError(f"Video {phase} manifest archive collision: {archive}")
        path.unlink()
    else:
        os.replace(path, archive)
    print(f"Archived superseded video {phase} manifest: {archive}")
    return archive


def terminal_failure_detail(state: TerminalJobState) -> str:
    parts = [
        f"shot {state.shot_id}={state.transport_status}",
        f"transport_job_id={state.transport_job_id or '<none>'}",
    ]
    if state.last_terminal_payload is not None:
        rendered = json.dumps(
            state.last_terminal_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if len(rendered) > 1800:
            rendered = rendered[:1797] + "..."
        parts.append(f"provider_payload={rendered}")
    return " ".join(parts)


def download_verified_mp4(
    storage: ObjectStorage,
    artifact: OutputArtifact,
    destination: Path,
    *,
    shot_id: int,
    kind: Literal["video", "upscaled video"],
) -> None:
    valid_local = (
        destination.is_file()
        and destination.stat().st_size == artifact.size_bytes
        and sha256_file(destination) == artifact.sha256
    )
    if not valid_local:
        storage.download(artifact.key, destination)
    if destination.stat().st_size != artifact.size_bytes:
        raise ValueError(f"Downloaded {kind} size mismatch for shot {shot_id}")
    if sha256_file(destination) != artifact.sha256:
        raise ValueError(f"Downloaded {kind} SHA mismatch for shot {shot_id}")
