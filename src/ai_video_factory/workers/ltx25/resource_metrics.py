"""Read-only process/cgroup resource metrics for opt-in A2V Full HD stage2 profiling.

Salad containers may expose v1, v2, or no readable memory cgroup mount.
Never emit an empty diagnostics record: process RSS / major faults and
process I/O fallbacks are still useful when cgroup metrics are unavailable.
No checkpoint reads, system calls that alter file cache, or memory pinning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _integer(path: Path) -> int | None:
    value = _text(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _stats(path: Path) -> dict[str, int]:
    text = _text(path)
    if text is None:
        return {}
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            # /proc/self/io uses "read_bytes: N"; cgroup memory.stat uses "key N".
            values[parts[0].removesuffix(":")] = int(parts[1])
        except ValueError:
            continue
    return values


def _cgroup_paths(proc_root: Path, cgroup_root: Path) -> list[tuple[str, Path, str]]:
    """Resolve cgroup namespaces through mountinfo; retain safe conventional fallbacks.

    /proc/self/cgroup paths refer to the cgroup filesystem root, not necessarily
    the container's visible mount root. Linux mountinfo gives BOTH the mount
    point and the mounted subtree root needed to map a nested path correctly.
    """
    memberships: list[tuple[str, str]] = []
    for line in (_text(proc_root / "cgroup") or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _, subsystems, path = parts
        if not subsystems:
            memberships.append(("v2", path))
        elif "memory" in subsystems.split(","):
            memberships.append(("v1", path))

    candidates: list[tuple[str, Path, str]] = []
    # Prefer the explicitly mounted hierarchy, accounting for cgroupns roots
    # and a bind mount whose hierarchy is deeper than '/'. Mountinfo uses
    # escaped spaces in paths (\\040) -- not material for the normal cgroup
    # mount but handle them to avoid misparsing valid Linux mountinfo.
    for line in (_text(proc_root / "mountinfo") or "").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields, after = left.split(), right.split()
        if len(fields) < 5 or len(after) < 3:
            continue
        mounted_root = fields[3].replace("\\040", " ")
        mount = fields[4].replace("\\040", " ")
        fs_type = after[0]
        for version, member in memberships:
            if (version == "v2" and fs_type != "cgroup2") or (
                version == "v1" and (
                    fs_type != "cgroup" or "memory" not in after[2].split(",")
                )
            ):
                continue
            # A container may expose only a subtree of the host hierarchy.
            root = mounted_root.rstrip("/") or "/"
            if member == root:
                relative = ""
            elif root == "/":
                relative = member.lstrip("/")
            elif member.startswith(root + "/"):
                relative = member[len(root):].lstrip("/")
            else:
                continue
            mountpoint = Path(mount)
            # Support alternate proc/cgroup fixtures without accessing
            # the host cgroup mount in unit tests.
            if cgroup_root != Path("/sys/fs/cgroup"):
                try:
                    mountpoint = cgroup_root / mountpoint.relative_to("/sys/fs/cgroup")
                except ValueError:
                    continue
            candidates.append((version, mountpoint / relative, "mountinfo"))

    for version, member in memberships:
        if version == "v2":
            candidates.append((version, cgroup_root / member.lstrip("/"), "membership"))
        else:
            candidates.append((
                version, cgroup_root / "memory" / member.lstrip("/"), "membership",
            ))
    candidates.extend([
        ("v2", cgroup_root, "root_fallback"),
        ("v1", cgroup_root / "memory", "root_fallback"),
        ("v1", cgroup_root, "root_fallback"),
    ])
    # Avoid redundant filesystem reads during continuous sampling.
    return list(dict.fromkeys(candidates))


def memory_pressure(
    *,
    proc_root: Path = Path("/proc/self"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any]:
    """Resource snapshot robust to Salad's cgroup namespaces and mount policy."""
    result: dict[str, Any] = {"cgroup_available": False, "process_available": False}
    status = _text(proc_root / "status") or ""
    for line in status.splitlines():
        key, _, val = line.partition(":")
        if key not in ("VmRSS", "VmHWM", "VmSwap"):
            continue
        parts = val.split()
        if parts:
            try:
                result[f"process_{key.lower()}_bytes"] = int(parts[0]) * 1024
                result["process_available"] = True
            except ValueError:
                pass

    stat = _text(proc_root / "stat")
    if stat and ") " in stat:
        # Linux proc stat uses (comm) with spaces, so split AFTER the last ')'.
        fields = stat.rsplit(") ", 1)[1].split()
        if len(fields) >= 10:
            try:
                result["process_minor_page_faults"] = int(fields[7])
                result["process_major_page_faults"] = int(fields[9])
                result["process_available"] = True
            except ValueError:
                pass

    io_stats = _stats(proc_root / "io")
    for key in ("read_bytes", "rchar", "syscr"):
        if key in io_stats:
            result[f"process_{key}"] = io_stats[key]
            result["process_available"] = True

    for version, base, source in _cgroup_paths(proc_root, cgroup_root):
        current_file = "memory.current" if version == "v2" else "memory.usage_in_bytes"
        usage = _integer(base / current_file)
        if usage is None:
            continue
        result["cgroup_available"] = True
        result["cgroup_version"] = version
        result["cgroup_resolution"] = source
        result["cgroup_memory_current_bytes"] = usage
        # Current RSS alone is not enough to evaluate Salad placement.
        # Read the container's own cgroup limits/peaks when exposed;
        # v2 uses 'max' for unlimited and v1 uses an enormous sentinel.
        limit = _integer(base / ("memory.max" if version == "v2" else "memory.limit_in_bytes"))
        if limit is not None and 0 < limit < (1 << 60):
            result["cgroup_memory_limit_bytes"] = limit
        peak = _integer(
            base / ("memory.peak" if version == "v2" else "memory.max_usage_in_bytes")
        )
        if peak is not None:
            result["cgroup_memory_peak_bytes"] = peak
        if version == "v2":
            events = _stats(base / "memory.events")
            for event in ("oom", "oom_kill", "high", "max"):
                if event in events:
                    result[f"cgroup_memory_events_{event}"] = events[event]
        if version == "v2":
            swap_peak = _integer(base / "memory.swap.peak")
            swap = _integer(base / "memory.swap.current")
            if swap_peak is not None:
                result["cgroup_swap_peak_bytes"] = swap_peak
            if swap is not None:
                result["cgroup_swap_counter_bytes"] = swap
        else:
            # v1 memsw is memory PLUS swap: not the same as v2
            # memory.swap.current. Subtract contemporaneous memory usage.
            memsw_peak = _integer(base / "memory.memsw.max_usage_in_bytes")
            if memsw_peak is not None:
                result["cgroup_memsw_reported_peak_bytes"] = memsw_peak
            memsw = _integer(base / "memory.memsw.usage_in_bytes")
            if memsw is not None:
                result["cgroup_memsw_current_bytes"] = memsw
                result["cgroup_swap_counter_bytes"] = max(0, memsw - usage)
        info = _stats(base / "memory.stat")
        mapping = {
            "file": "file" if version == "v2" else "total_cache",
            "anon": "anon" if version == "v2" else "total_rss",
            "pgmajfault": "pgmajfault" if version == "v2" else "total_pgmajfault",
            "pgscan": "pgscan" if version == "v2" else "total_pgscan",
            "pgsteal": "pgsteal" if version == "v2" else "total_pgsteal",
        }
        for target, source in mapping.items():
            if source in info:
                result[f"cgroup_{target}"] = info[source]
        break
    if not result["cgroup_available"]:
        result["cgroup_unavailable_reason"] = "memory_cgroup_files_not_exposed_or_unreadable"
    return result



def cuda_memory_snapshot() -> dict[str, Any]:
    """Best-effort read-only CUDA telemetry, including when an OOM was raised.

    Failures obtaining metrics must never mask the actual inference exception.
    Import torch only on GPU workers so lightweight CI remains torch-free.
    """
    try:
        import torch
    except ImportError:
        return {"cuda_available": False}
    try:
        if not torch.cuda.is_available():
            return {"cuda_available": False}
        free, total = torch.cuda.mem_get_info()
        return {
            "cuda_available": True,
            "free_bytes": free,
            "total_bytes": total,
            "allocated_bytes": torch.cuda.memory_allocated(),
            "reserved_bytes": torch.cuda.memory_reserved(),
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        }
    except Exception as exc:
        return {
            "cuda_available": True,
            "telemetry_error": type(exc).__name__,
        }


def _monotonic_counter_delta(
    initial: dict[str, Any],
    after: dict[str, Any],
    key: str,
) -> int | None:
    before = initial.get(key)
    current = after.get(key)
    if (
        not isinstance(before, int)
        or not isinstance(current, int)
        or current < before
    ):
        return None
    return current - before


def check_stage1_host_pressure(
    initial: dict[str, Any],
    after: dict[str, Any],
    max_swap_growth_bytes: int,
    metrics: dict[str, Any] | None = None,
    *,
    major_page_faults_diagnostic_limit: int = 0,
) -> None:
    """Fail before Stage 2 only on evidence of real host-memory pressure.

    Major faults are diagnostic: cold file-backed mmap/safetensors reads can
    legitimately generate many major faults without swap or memory pressure.
    The hard guard instead uses process/cgroup swap growth and cgroup memory
    events. Reclaim counters remain diagnostic because file-cache reclaim can
    also accompany healthy checkpoint streaming.
    """
    if max_swap_growth_bytes < 0:
        raise ValueError("A2V stage1 swap-growth limit cannot be negative")
    if major_page_faults_diagnostic_limit < 0:
        raise ValueError("A2V stage1 major-page-fault diagnostic cannot be negative")

    fault_delta = _monotonic_counter_delta(
        initial, after, "process_major_page_faults"
    )
    process_swap_delta = _monotonic_counter_delta(
        initial, after, "process_vmswap_bytes"
    )

    same_cgroup = (
        initial.get("cgroup_available") is True
        and after.get("cgroup_available") is True
        and initial.get("cgroup_version") == after.get("cgroup_version")
    )
    cgroup_swap_delta = None
    cgroup_swap_peak_delta = None
    event_deltas: dict[str, int | None] = {}
    reclaim_deltas: dict[str, int | None] = {}
    if same_cgroup:
        cgroup_swap_delta = _monotonic_counter_delta(
            initial, after, "cgroup_swap_counter_bytes"
        )
        cgroup_swap_peak_delta = _monotonic_counter_delta(
            initial, after, "cgroup_swap_peak_bytes"
        )
        for event in ("high", "max", "oom", "oom_kill"):
            event_deltas[event] = _monotonic_counter_delta(
                initial, after, f"cgroup_memory_events_{event}"
            )
        for counter in ("pgscan", "pgsteal"):
            reclaim_deltas[counter] = _monotonic_counter_delta(
                initial, after, f"cgroup_{counter}"
            )

    fault_limit_exceeded = (
        fault_delta is not None
        and major_page_faults_diagnostic_limit > 0
        and fault_delta > major_page_faults_diagnostic_limit
    )
    reasons: list[str] = []
    for label, delta in (
        ("process_swap_growth_bytes", process_swap_delta),
        ("cgroup_swap_growth_bytes", cgroup_swap_delta),
        ("cgroup_swap_peak_growth_bytes", cgroup_swap_peak_delta),
    ):
        if delta is not None and delta > max_swap_growth_bytes:
            reasons.append(f"{label}={delta}>{max_swap_growth_bytes}")
    for event, delta in event_deltas.items():
        if delta is not None and delta > 0:
            reasons.append(f"cgroup_memory_events_{event}_delta={delta}")

    if metrics is not None:
        metrics["stage1_major_page_faults_delta"] = fault_delta
        # Keep the old key for report compatibility, but it is diagnostic only.
        metrics["stage1_major_page_faults_limit"] = (
            major_page_faults_diagnostic_limit
        )
        metrics["stage1_major_page_faults_exceeded_diagnostic"] = (
            fault_limit_exceeded
        )
        metrics["stage1_process_swap_growth_bytes"] = process_swap_delta
        metrics["stage1_cgroup_swap_growth_bytes"] = cgroup_swap_delta
        metrics["stage1_cgroup_swap_peak_growth_bytes"] = cgroup_swap_peak_delta
        metrics["stage1_max_swap_growth_bytes"] = max_swap_growth_bytes
        metrics["stage1_cgroup_memory_event_deltas"] = event_deltas
        metrics["stage1_cgroup_reclaim_deltas"] = reclaim_deltas
        metrics["stage1_host_pressure_detected"] = bool(reasons)
        metrics["stage1_host_pressure_reasons"] = reasons

    if reasons:
        raise RuntimeError(
            "A2V latency guard: real host memory pressure after stage1 ("
            + ", ".join(reasons)
            + "). No stage2 or VAE was started; inspect Salad host memory."
        )
