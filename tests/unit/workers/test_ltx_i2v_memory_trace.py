"""Free CPU-only checks for per-request I2V RAM timelines and cgroupns discovery."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ai_video_factory.workers.ltx25.memory_trace import (
    I2VMemoryTrace,
    mark_i2v_ram_phase,
)
from ai_video_factory.workers.ltx25.resource_metrics import memory_pressure


def test_mountinfo_resolves_nested_cgroup_v2_even_when_root_is_hidden(
    tmp_path: Path,
) -> None:
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir()
    (cg / "nested").mkdir(parents=True)
    (proc / "cgroup").write_text("0::/host/job/nested\n")
    (proc / "mountinfo").write_text(
        "42 41 0:99 /host/job /sys/fs/cgroup rw,nosuid - cgroup2 cgroup rw\n"
    )
    (proc / "status").write_text("VmRSS:\t1024 kB\nVmSwap:\t3 kB\n")
    (cg / "nested/memory.current").write_text("123456\n")
    (cg / "nested/memory.max").write_text("64424509440\n")
    (cg / "nested/memory.peak").write_text("200000\n")
    result = memory_pressure(proc_root=proc, cgroup_root=cg)
    assert result["cgroup_available"]
    assert result["cgroup_resolution"] == "mountinfo"
    assert result["cgroup_memory_current_bytes"] == 123456
    assert result["cgroup_memory_limit_bytes"] == 64424509440
    assert result["process_vmrss_bytes"] == 1024 * 1024


def test_mountinfo_resolves_v1_bind_root_and_collects_swap(
    tmp_path: Path,
) -> None:
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir()
    cg.mkdir()
    (proc / "cgroup").write_text("4:memory:/kubepods/container\n")
    (proc / "mountinfo").write_text(
        "16 15 0:42 /kubepods/container /sys/fs/cgroup/memory "
        "rw,nosuid - cgroup cgroup rw,memory\n"
    )
    (cg / "memory").mkdir()
    (cg / "memory/memory.usage_in_bytes").write_text("1000000\n")
    (cg / "memory/memory.memsw.usage_in_bytes").write_text("1500000\n")
    result = memory_pressure(proc_root=proc, cgroup_root=cg)
    assert result["cgroup_version"] == "v1"
    assert result["cgroup_resolution"] == "mountinfo"
    assert result["cgroup_memory_current_bytes"] == 1000000
    assert result["cgroup_memsw_current_bytes"] == 1500000
    assert result["cgroup_swap_counter_bytes"] == 500000


def test_missing_cgroup_never_fakes_container_ram(
    tmp_path: Path,
) -> None:
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir()
    cg.mkdir()
    (proc / "status").write_text("VmRSS:\t144 kB\nVmHWM:\t156 kB\n")
    record = memory_pressure(proc_root=proc, cgroup_root=cg)
    assert record["process_vmrss_bytes"] == 144 * 1024
    assert record["cgroup_available"] is False
    assert record["cgroup_unavailable_reason"]


def test_ram_timeline_samples_phases_headroom_and_kernel_peaks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    readings = iter((
        {
            "process_available": True, "cgroup_available": True,
            "cgroup_resolution": "mountinfo",
            "process_vmrss_bytes": 100, "process_vmhwm_bytes": 180,
            "process_vmswap_bytes": 10, "process_major_page_faults": 5,
            "cgroup_memory_current_bytes": 220, "cgroup_memory_peak_bytes": 250,
            "cgroup_memory_limit_bytes": 500, "cgroup_swap_counter_bytes": 12,
            "cgroup_memory_events_oom": 0,
        },
        {
            "process_available": True, "cgroup_available": True,
            "process_vmrss_bytes": 300, "process_vmhwm_bytes": 330,
            "process_vmswap_bytes": 20, "process_major_page_faults": 14,
            "cgroup_memory_current_bytes": 410, "cgroup_memory_peak_bytes": 440,
            "cgroup_memory_limit_bytes": 500, "cgroup_swap_counter_bytes": 30,
            "cgroup_memory_events_oom": 0,
        },
        {
            "process_available": True, "cgroup_available": True,
            "process_vmrss_bytes": 150, "process_vmhwm_bytes": 330,
            "process_vmswap_bytes": 3, "process_major_page_faults": 22,
            "cgroup_memory_current_bytes": 250, "cgroup_memory_peak_bytes": 440,
            "cgroup_memory_limit_bytes": 500, "cgroup_swap_counter_bytes": 5,
            "cgroup_memory_events_oom": 1,
        },
    ))
    # The collector deliberately takes one final snapshot when exiting.
    # Reuse the latest reading after the three synthetic phase transitions.
    latest: dict = {}

    def reader() -> dict:
        nonlocal latest
        latest = next(readings, latest)
        return latest

    with caplog.at_level(logging.INFO), I2VMemoryTrace(
        interval_seconds=120, reader=reader,
    ) as monitor:
        mark_i2v_ram_phase("stage1_denoise")
        mark_i2v_ram_phase("stage2_transformer_build")
    report = monitor.report()
    assert report["sample_count"] == 4
    assert len([r for r in caplog.records if "LTX25_I2V_RAM_SAMPLE {" in r.message]) == 3
    assert report["process_sampled_peak_rss_bytes"] == 300
    assert report["process_reported_peak_hwm_bytes"] == 330
    assert report["cgroup_sampled_peak_bytes"] == 410
    assert report["cgroup_reported_peak_bytes"] == 440
    assert report["cgroup_min_sampled_headroom_bytes"] == 90
    assert report["process_major_fault_delta"] == 17
    assert report["cgroup_oom_event_delta"] == 1
    assert report["per_phase_sampled_peaks"]["stage1_denoise"]["process_vmrss_bytes"] == 300
    assert (
        report["per_phase_sampled_peaks"]["stage2_transformer_build"]["process_vmrss_bytes"]
        == 150
    )
    assert [s["phase"] for s in report["samples"]] == [
        "start", "stage1_denoise", "stage2_transformer_build",
        "stage2_transformer_build",
    ]
    json.dumps(report)
    assert report["cgroup_available_during_run"]
    assert report["sample_errors"] == 0


def test_trace_stays_read_only_and_falls_back_when_sampling_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken() -> dict:
        raise OSError("unavailable")

    with caplog.at_level(logging.WARNING), I2VMemoryTrace(
        interval_seconds=120, reader=broken,
    ) as trace:
        mark_i2v_ram_phase("stage1")
    assert trace.summary()["sample_errors"] >= 2
    assert trace.summary()["sample_count"] == 0
    assert not trace.summary()["cgroup_available_during_run"]
    assert "LTX25_I2V_RAM_SAMPLE_FAILED" in caplog.text


def test_trace_default_is_noop_outside_pair() -> None:
    mark_i2v_ram_phase("stage1")
    with pytest.raises(ValueError, match="interval"):
        I2VMemoryTrace(interval_seconds=0.01)
