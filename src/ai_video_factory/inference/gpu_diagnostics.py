"""Best-effort, per-attempt A2V diagnostics persisted outside ephemeral Salad logs.

Checkpoints are immutable JSON objects in R2 (or the configured local object store).
They contain only operational telemetry: no prompts, audio, image bytes, or credentials.
A worker killed without a Python traceback still leaves its last completed checkpoint.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import traceback
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ports import ObjectStorage

logger = logging.getLogger(__name__)

_ACTIVE_DIAGNOSTICS: ContextVar[GpuJobDiagnostics | None] = ContextVar(
    "active_ltx_a2v_diagnostics", default=None
)
_SAFE_PHASE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")


def _read_linux_memory() -> dict[str, int | str]:
    values: dict[str, int | str] = {}
    paths = (
        ("/proc/self/status", ("VmRSS", "VmHWM", "VmPin", "VmLck")),
        ("/proc/meminfo", ("MemAvailable",)),
    )
    for filename, names in paths:
        try:
            lines = Path(filename).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            key, _, raw = line.partition(":")
            if key in names:
                with suppress(ValueError, IndexError):
                    values[key.lower() + "_kb"] = int(raw.split()[0])
    for key, filename in (
        ("cgroup_memory_current_bytes", "/sys/fs/cgroup/memory.current"),
        ("cgroup_memory_max_bytes", "/sys/fs/cgroup/memory.max"),
    ):
        with suppress(OSError):
            value = Path(filename).read_text(encoding="utf-8").strip()
            values[key] = int(value) if value.isdigit() else value
    return values


def _cuda_snapshot() -> dict[str, Any]:
    """Inspect allocator and device without allocating CUDA tensors or synchronizing."""
    try:
        import torch
    except ImportError:
        return {"available": False, "reason": "torch_not_installed"}
    result: dict[str, Any] = {}
    try:
        if not torch.cuda.is_available():
            return {"available": False}
        index = torch.cuda.current_device()
        result["available"] = True
        result["device_index"] = index
        operations = (
            ("mem_get_info", lambda: torch.cuda.mem_get_info(index)),
            ("memory_allocated_bytes", lambda: torch.cuda.memory_allocated(index)),
            ("memory_reserved_bytes", lambda: torch.cuda.memory_reserved(index)),
            ("max_memory_allocated_bytes", lambda: torch.cuda.max_memory_allocated(index)),
            ("max_memory_reserved_bytes", lambda: torch.cuda.max_memory_reserved(index)),
            (
                "per_process_memory_fraction",
                lambda: torch.cuda.get_per_process_memory_fraction(index),
            ),
            ("device_name", lambda: torch.cuda.get_device_name(index)),
        )
        for name, operation in operations:
            try:
                value = operation()
                if name == "mem_get_info":
                    result["device_free_bytes"], result["device_total_bytes"] = map(int, value)
                else:
                    result[name] = value
            except Exception as error:
                result[name + "_error"] = type(error).__name__
        try:
            stats = torch.cuda.memory_stats(index)
            for name in (
                "num_ooms",
                "num_alloc_retries",
                "active_bytes.all.peak",
                "reserved_bytes.all.peak",
            ):
                if name in stats:
                    result[name.replace(".", "_")] = int(stats[name])
        except Exception as error:
            result["memory_stats_error"] = type(error).__name__
    except Exception as error:
        result["snapshot_error"] = type(error).__name__
    return result


def _gpu_identity() -> dict[str, str]:
    """NVML-backed nvidia-smi values if available; never include environment secrets."""
    try:
        process = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,pci.bus_id,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if process.returncode == 0:
            return {"nvidia_smi": process.stdout.strip()[:2048]}
        return {"nvidia_smi_error": f"exit_{process.returncode}"}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"nvidia_smi_error": type(error).__name__}


class GpuJobDiagnostics:
    """Capture phase, host/CUDA metrics, and traceback into immutable per-attempt R2 keys."""

    def __init__(
        self,
        *,
        storage: ObjectStorage,
        work_dir: Path,
        job_id: str,
        attempt_count: int,
        worker_id: str,
        instance_id: str | None,
        generation_profile: str,
    ) -> None:
        self.storage = storage
        self.work_dir = work_dir
        self.job_id = job_id
        self.attempt_count = attempt_count
        self.worker_id = worker_id
        self.instance_id = instance_id
        self.generation_profile = generation_profile
        self.last_phase = "claimed"
        self.last_key: str | None = None
        self.sequence = 0
        self.identity = _gpu_identity()

    def checkpoint(self, phase: str, *, sync_cuda: bool = False) -> None:
        if not _SAFE_PHASE.fullmatch(phase):
            raise ValueError("diagnostic phase must be a fixed safe identifier")
        # A synchronous boundary attributes asynchronous kernel faults to the
        # preceding phase instead of reporting a misleading subsequent call.
        if sync_cuda:
            try:
                import torch
            except ImportError:
                pass
            else:
                if torch.cuda.is_available():
                    torch.cuda.synchronize()
        self.last_phase = phase
        self._persist(phase, None)

    def failure(self, error: BaseException) -> None:
        prior_phase = self.last_phase
        trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        self._persist(
            "failed",
            {
                "failed_during_phase": prior_phase,
                "exception_type": type(error).__name__,
                "exception_message": str(error)[:4096],
                "traceback": trace[-65536:],
            },
        )
        with suppress(Exception):
            error._gpu_diagnostic_phase = prior_phase
            error._gpu_diagnostic_key = self.last_key
        logger.exception(
            "LTX_A2V_DIAGNOSTIC_FAILURE job_id=%s attempt=%d phase=%s r2_key=%s",
            self.job_id,
            self.attempt_count,
            prior_phase,
            self.last_key or "<unavailable>",
            exc_info=(type(error), error, error.__traceback__),
        )

    def _persist(self, phase: str, failure: dict[str, str] | None) -> None:
        self.sequence += 1
        key = (
            f"diagnostics/ltx-a2v/{self.job_id}/"
            f"attempt-{self.attempt_count:02d}/{self.sequence:03d}-{phase.replace('.', '-')}.json"
        )
        payload: dict[str, Any] = {
            "schema": "ltx-a2v-gpu-diagnostics-v1",
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "job_id": self.job_id,
            "attempt_count": self.attempt_count,
            "worker_id": self.worker_id,
            "instance_id": self.instance_id,
            "machine_id": os.getenv("SALAD_MACHINE_ID") or None,
            "generation_profile": self.generation_profile,
            "phase": phase,
            "last_phase": self.last_phase,
            "gpu_identity": self.identity,
            "cuda": _cuda_snapshot(),
            "host_memory": _read_linux_memory(),
        }
        if failure is not None:
            payload["failure"] = failure
        logger.info(
            "LTX_A2V_DIAGNOSTIC job_id=%s attempt=%d phase=%s r2_key=%s cuda=%s",
            self.job_id,
            self.attempt_count,
            phase,
            key,
            json.dumps(payload["cuda"], sort_keys=True),
        )
        try:
            folder = self.work_dir / "gpu-diagnostics"
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder / f"{self.sequence:03d}.json"
            destination.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.storage.create_if_absent(
                destination,
                key,
                content_type="application/json",
                metadata={
                    "job-id": self.job_id,
                    "attempt": str(self.attempt_count),
                    "phase": phase,
                },
            )
        except Exception as upload_error:
            logger.warning(
                "LTX_A2V_DIAGNOSTIC_UPLOAD_FAILED job_id=%s attempt=%d phase=%s "
                "error_type=%s",
                self.job_id,
                self.attempt_count,
                phase,
                type(upload_error).__name__,
            )
        else:
            self.last_key = key


@contextmanager
def gpu_diagnostics_scope(diagnostics: GpuJobDiagnostics) -> Iterator[None]:
    token = _ACTIVE_DIAGNOSTICS.set(diagnostics)
    try:
        yield
    finally:
        _ACTIVE_DIAGNOSTICS.reset(token)


def diagnostic_checkpoint(phase: str, *, sync_cuda: bool = False) -> None:
    current = _ACTIVE_DIAGNOSTICS.get()
    if current is not None:
        current.checkpoint(phase, sync_cuda=sync_cuda)
