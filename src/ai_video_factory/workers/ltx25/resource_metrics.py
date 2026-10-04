"""Read-only process/cgroup resource metrics for the unmerged I2V speed trial.

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
        swap_peak = _integer(
            base / ("memory.swap.peak" if version == "v2" else "memory.memsw.max_usage_in_bytes")
        )
        if swap_peak is not None:
            result["cgroup_swap_peak_bytes"] = swap_peak
        swap = _integer(
            base / ("memory.swap.current" if version == "v2" else "memory.memsw.usage_in_bytes")
        )
        if swap is not None:
            result["cgroup_swap_counter_bytes"] = swap
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
