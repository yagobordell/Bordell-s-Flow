from __future__ import annotations

import hashlib
import statistics
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ai_video_factory.domain.gpu import (
    GPUDeviceProfile,
    LTXBenchmarkProfile,
    LTXBenchmarkReport,
    LTXBenchmarkSample,
)

_OUTPUT_PLACEHOLDER = "{output}"
_SECRET_FLAGS = {
    "--access-key",
    "--api-key",
    "--hf-token",
    "--password",
    "--secret-access-key",
    "--secret-key",
    "--token",
}
_SECRET_ENVIRONMENT_KEYS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "HF_TOKEN",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "SALAD_API_KEY",
}


class BenchmarkCommandError(RuntimeError):
    """Raised when the benchmarked process exits unsuccessfully."""

    def __init__(self, command: Sequence[str], return_code: int) -> None:
        super().__init__(f"Benchmark command failed with exit code {return_code}: {command[0]}")
        self.command = tuple(command)
        self.return_code = return_code


@dataclass(frozen=True)
class CommandMeasurement:
    """Raw process measurement before it becomes a persisted benchmark sample."""

    duration_seconds: float
    peak_gpu_memory_mib: tuple[int, ...]
    output_size_bytes: int
    output_sha256: str


class GPUProbe(Protocol):
    """Provider-neutral view of GPU identity and live memory usage."""

    def discover_devices(self) -> list[GPUDeviceProfile]:
        """Return devices in the same stable order used by memory samples."""
        ...

    def memory_used_mib(self) -> list[int]:
        """Return current memory use for every discovered device."""
        ...


class BenchmarkExecutor(Protocol):
    """Executes one command while observing GPU memory."""

    def discover_devices(self) -> list[GPUDeviceProfile]:
        """Return the hardware profile used for all subsequent runs."""
        ...

    def run(
        self,
        command: Sequence[str],
        *,
        output_path: Path,
        sample_interval_seconds: float,
    ) -> CommandMeasurement:
        """Run one benchmark command and validate its generated artifact."""
        ...


class NvidiaSMIProbe:
    """Read deterministic GPU identity and memory data from nvidia-smi."""

    def __init__(self, executable: str = "nvidia-smi") -> None:
        self._executable = executable

    def discover_devices(self) -> list[GPUDeviceProfile]:
        result = self._query("index,name,memory.total")
        devices: list[GPUDeviceProfile] = []

        for line in _non_empty_lines(result):
            parts = [part.strip() for part in line.split(",", maxsplit=2)]
            if len(parts) != 3:
                raise RuntimeError(f"Unexpected nvidia-smi device row: {line}")
            devices.append(
                GPUDeviceProfile(
                    index=int(parts[0]),
                    name=parts[1],
                    memory_total_mib=int(parts[2]),
                )
            )

        if not devices:
            raise RuntimeError("nvidia-smi reported no GPU devices")
        return devices

    def memory_used_mib(self) -> list[int]:
        result = self._query("memory.used")
        values = [int(line.strip()) for line in _non_empty_lines(result)]
        if not values:
            raise RuntimeError("nvidia-smi reported no GPU memory samples")
        return values

    def _query(self, fields: str) -> str:
        try:
            result = subprocess.run(
                [
                    self._executable,
                    f"--query-gpu={fields}",
                    "--format=csv,noheader,nounits",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as error:
            raise RuntimeError(f"GPU probe executable not found: {self._executable}") from error
        except subprocess.CalledProcessError as error:
            message = error.stderr.strip() or "unknown nvidia-smi error"
            raise RuntimeError(f"GPU probe failed: {message}") from error
        return result.stdout


class SubprocessBenchmarkExecutor:
    """Run a command directly without a shell and sample GPU memory until it exits."""

    def __init__(self, gpu_probe: GPUProbe | None = None) -> None:
        self._gpu_probe = gpu_probe or NvidiaSMIProbe()
        self._device_count: int | None = None

    def discover_devices(self) -> list[GPUDeviceProfile]:
        devices = self._gpu_probe.discover_devices()
        self._device_count = len(devices)
        return devices

    def run(
        self,
        command: Sequence[str],
        *,
        output_path: Path,
        sample_interval_seconds: float,
    ) -> CommandMeasurement:
        if not command:
            raise ValueError("Benchmark command must not be empty")
        if sample_interval_seconds <= 0:
            raise ValueError("GPU sample interval must be positive")
        if self._device_count is None:
            raise RuntimeError("GPU devices must be discovered before running a benchmark")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        started_at = time.perf_counter()

        try:
            process = subprocess.Popen(list(command))
        except FileNotFoundError as error:
            raise RuntimeError(f"Benchmark executable not found: {command[0]}") from error

        peaks = [0] * self._device_count
        try:
            while process.poll() is None:
                _merge_memory_peaks(peaks, self._gpu_probe.memory_used_mib())
                time.sleep(sample_interval_seconds)
            _merge_memory_peaks(peaks, self._gpu_probe.memory_used_mib())
        except BaseException:
            if process.poll() is None:
                process.terminate()
                process.wait()
            raise

        duration_seconds = time.perf_counter() - started_at
        if process.returncode != 0:
            raise BenchmarkCommandError(command, process.returncode)
        if not output_path.is_file():
            raise RuntimeError(f"Benchmark command did not create its output: {output_path}")

        output_size_bytes = output_path.stat().st_size
        if output_size_bytes <= 0:
            raise RuntimeError(f"Benchmark command created an empty output: {output_path}")

        return CommandMeasurement(
            duration_seconds=duration_seconds,
            peak_gpu_memory_mib=tuple(peaks),
            output_size_bytes=output_size_bytes,
            output_sha256=_sha256_file(output_path),
        )


def benchmark_ltx_command(
    profile: LTXBenchmarkProfile,
    command_template: Sequence[str],
    *,
    output_path: Path,
    executor: BenchmarkExecutor | None = None,
    sample_interval_seconds: float = 0.2,
) -> LTXBenchmarkReport:
    """Execute warmup and measured LTX runs and return one reproducible report."""

    command = _render_command(command_template, output_path)
    benchmark_executor = executor or SubprocessBenchmarkExecutor()
    devices = benchmark_executor.discover_devices()
    if not devices:
        raise RuntimeError("Benchmark executor reported no GPU devices")

    for _ in range(profile.warmup_runs):
        benchmark_executor.run(
            command,
            output_path=output_path,
            sample_interval_seconds=sample_interval_seconds,
        )

    samples = [
        _to_sample(
            run_index,
            benchmark_executor.run(
                command,
                output_path=output_path,
                sample_interval_seconds=sample_interval_seconds,
            ),
        )
        for run_index in range(1, profile.measured_runs + 1)
    ]
    _validate_sample_device_counts(samples, devices)

    durations = [sample.duration_seconds for sample in samples]
    mean_duration = statistics.fmean(durations)
    return LTXBenchmarkReport(
        created_at=datetime.now(UTC),
        profile=profile,
        command=_redact_command(command_template),
        devices=devices,
        samples=samples,
        mean_duration_seconds=mean_duration,
        median_duration_seconds=statistics.median(durations),
        min_duration_seconds=min(durations),
        max_duration_seconds=max(durations),
        mean_generated_frames_per_second=profile.num_frames / mean_duration,
        max_peak_gpu_memory_mib=[
            max(sample.peak_gpu_memory_mib[index] for sample in samples)
            for index in range(len(devices))
        ],
    )


def _render_command(command_template: Sequence[str], output_path: Path) -> list[str]:
    if not command_template:
        raise ValueError("Benchmark command must not be empty")

    placeholder_count = sum(token.count(_OUTPUT_PLACEHOLDER) for token in command_template)
    if placeholder_count != 1:
        raise ValueError("Benchmark command must contain exactly one {output} placeholder")
    return [token.replace(_OUTPUT_PLACEHOLDER, str(output_path)) for token in command_template]


def _redact_command(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False

    for token in command:
        if redact_next:
            redacted.append("***")
            redact_next = False
            continue

        lowered = token.lower()
        key, separator, _ = token.partition("=")
        if separator and key.upper() in _SECRET_ENVIRONMENT_KEYS:
            redacted.append(f"{key}=***")
            continue

        matching_flag = next(
            (flag for flag in _SECRET_FLAGS if lowered.startswith(f"{flag}=")),
            None,
        )
        if matching_flag is not None:
            redacted.append(f"{token.split('=', maxsplit=1)[0]}=***")
            continue

        redacted.append(token)
        if lowered in _SECRET_FLAGS:
            redact_next = True

    return redacted


def _to_sample(run_index: int, measurement: CommandMeasurement) -> LTXBenchmarkSample:
    return LTXBenchmarkSample(
        run_index=run_index,
        duration_seconds=measurement.duration_seconds,
        peak_gpu_memory_mib=list(measurement.peak_gpu_memory_mib),
        output_size_bytes=measurement.output_size_bytes,
        output_sha256=measurement.output_sha256,
    )


def _validate_sample_device_counts(
    samples: Sequence[LTXBenchmarkSample],
    devices: Sequence[GPUDeviceProfile],
) -> None:
    for sample in samples:
        if len(sample.peak_gpu_memory_mib) != len(devices):
            raise ValueError(
                f"Benchmark run {sample.run_index} has memory data for "
                f"{len(sample.peak_gpu_memory_mib)} devices; expected {len(devices)}"
            )


def _merge_memory_peaks(peaks: list[int], current: Sequence[int]) -> None:
    if len(current) != len(peaks):
        raise RuntimeError(
            f"nvidia-smi memory sample has {len(current)} devices; expected {len(peaks)}"
        )
    for index, value in enumerate(current):
        if value < 0:
            raise RuntimeError("nvidia-smi reported negative GPU memory usage")
        peaks[index] = max(peaks[index], value)


def _non_empty_lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.strip()]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
