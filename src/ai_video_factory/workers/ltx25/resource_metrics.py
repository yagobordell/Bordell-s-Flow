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
            values[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return values


def _cgroup_paths(proc_root: Path, cgroup_root: Path) -> list[tuple[str, Path]]:
    """Try explicitly mounted cgroup paths then the root mount as fallback."""
    discovered: list[tuple[str, Path]] = []
    proc_cgroup = _text(proc_root / "cgroup") or ""
    for line in proc_cgroup.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _, subsystem, path = parts
        if subsystem == "" or "memory" in subsystem.split(","):
            if subsystem == "":
                discovered.append(("v2", cgroup_root / path.lstrip("/")))
            else:
                discovered.append(("v1", cgroup_root / "memory" / path.lstrip("/")))
    discovered.extend([
        ("v2", cgroup_root),
        ("v1", cgroup_root / "memory"),
        ("v1", cgroup_root),
    ])
    return discovered


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

    for version, base in _cgroup_paths(proc_root, cgroup_root):
        current_file = "memory.current" if version == "v2" else "memory.usage_in_bytes"
        usage = _integer(base / current_file)
        if usage is None:
            continue
        result["cgroup_available"] = True
        result["cgroup_version"] = version
        result["cgroup_memory_current_bytes"] = usage
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
    return result
