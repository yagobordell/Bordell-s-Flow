"""Safely reconcile the pinned A2V Docker image before paying for LTX jobs.

Unlike the independent image upscaler, the Postgres Capacity Controller owns
LTX replica mutations. Never override its lock or replace an active worker.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_video_factory.inference.capacity_controller import (
    read_capacity_controller_health,
    validate_capacity_controller_dsn,
)
from ai_video_factory.inference.salad import SaladClient
from ai_video_factory.workers.ltx25.a2v import LTX_A2V_TASK

_PINNED = re.compile(r"^(?P<repo>[^ @]+)@sha256:[0-9a-fA-F]{64}$")


class LtxWorkerPrepareError(RuntimeError):
    """LTX image drift cannot be repaired without violating worker safety."""


def _nonterminal_a2v_jobs(dsn: str) -> int:
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as connection:
        row = connection.execute(
            """
            SELECT count(*) FROM gpu.jobs
            WHERE task = %s AND status IN ('pending', 'running', 'retryable_failed')
            """,
            (LTX_A2V_TASK,),
        ).fetchone()
    if row is None:
        raise LtxWorkerPrepareError("Could not inspect current A2V job reservations")
    return int(row[0])


def _prepare_command(
    *, repo_root: Path, expected: str, shell: str,
) -> list[str]:
    script = repo_root / "scripts" / "salad" / "manage_salad_worker.ps1"
    if not script.is_file() or script.is_symlink():
        raise LtxWorkerPrepareError("Trusted Salad worker manager is missing")
    return [
        shell, "-NoProfile", "-File", str(script),
        "-Action", "Prepare", "-Service", "ltx25", "-PinnedImage", expected,
        "-EnvFile", ".env", "-NonInteractive",
    ]


def ensure_a2v_image(
    *,
    client: SaladClient,
    expected: str,
    configured_repository: str,
    repo_root: Path,
    controller_dsn: str | None,
    auto_prepare: bool = True,
    check_controller: Callable[[str], Any] = read_capacity_controller_health,
    count_jobs: Callable[[str], int] = _nonterminal_a2v_jobs,
    find_executable: Callable[[str], str | None] = shutil.which,
    execute: Callable[..., Any] = subprocess.run,
) -> None:
    """Reuse a correct group; repair mismatch only when all guards are satisfied."""
    match = _PINNED.fullmatch(expected)
    if match is None or match["repo"] != configured_repository:
        raise LtxWorkerPrepareError(
            "Reviewed A2V digest is missing or references a different Docker repository"
        )
    group = client.describe_container_group()
    container = group.get("container")
    actual = container.get("image") if isinstance(container, dict) else None
    if actual == expected:
        return

    explanation = (
        f"Salad LTX A2V image mismatch: expected {expected}, deployed {actual!r}. "
        "No paid inference submitted."
    )
    if not auto_prepare:
        raise LtxWorkerPrepareError(
            explanation + " Automatic preparation is disabled; prepare ltx25 explicitly."
        )
    state = group.get("current_state")
    status = state.get("status") if isinstance(state, dict) else None
    if status != "stopped" or group.get("pending_change") is not False:
        raise LtxWorkerPrepareError(
            explanation + " The group is not stably stopped. Do not replace a live "
            "worker; drain its jobs and coordinate a maintenance window first."
        )
    if not controller_dsn:
        raise LtxWorkerPrepareError(
            explanation + " A session-mode controller Postgres DSN is required "
            "before attempting any automatic deployment."
        )
    try:
        dsn = validate_capacity_controller_dsn(controller_dsn)
        health = check_controller(dsn)
        if health.healthy:
            raise LtxWorkerPrepareError(
                explanation + " The Capacity Controller is active and owns the "
                "Salad mutation lock. Stop it during planned maintenance; "
                "then prepare the reviewed ltx25 digest and restart it."
            )
        pending = count_jobs(dsn)
    except LtxWorkerPrepareError:
        raise
    except Exception as exc:
        raise LtxWorkerPrepareError(
            explanation + " Cannot establish controller/job safety; refusing "
            "automatic deployment."
        ) from exc
    if pending:
        raise LtxWorkerPrepareError(
            explanation + f" {pending} unfinished A2V job(s) exist in Postgres. "
            "Reconcile them before replacing the worker."
        )

    shell = find_executable("pwsh") or (
        find_executable("powershell.exe") if os.name == "nt" else None
    )
    if shell is None:
        raise LtxWorkerPrepareError(
            explanation + " PowerShell is unavailable for the audited Prepare operation."
        )
    command = _prepare_command(repo_root=repo_root, expected=expected, shell=shell)
    print(
        "  LTX A2V: safely preparing the exact reviewed worker image "
        "under the existing Capacity Controller advisory lock...",
        flush=True,
    )
    try:
        execute(command, cwd=repo_root, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LtxWorkerPrepareError(
            explanation + " Automatic Prepare failed or lost the mutation lock. "
            "No LTX job was submitted."
        ) from exc
    verified = client.describe_container_group()
    current = verified.get("current_state")
    image = verified.get("container")
    if (
        not isinstance(image, dict) or image.get("image") != expected
        or not isinstance(current, dict) or current.get("status") != "stopped"
        or verified.get("pending_change") is not False
    ):
        raise LtxWorkerPrepareError(
            "Prepare finished, but Salad did not report the pinned A2V image "
            "in a stably stopped group; no LTX job was submitted."
        )
    print("  LTX A2V: reviewed worker prepared. Start the Capacity Controller.", flush=True)
