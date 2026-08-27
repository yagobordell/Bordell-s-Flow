import hashlib
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from ai_video_factory.domain.gpu import GPUDeviceProfile, LTXBenchmarkProfile
from ai_video_factory.workflows.gpu_benchmark import (
    BenchmarkCommandError,
    CommandMeasurement,
    SubprocessBenchmarkExecutor,
    benchmark_ltx_command,
)


class FakeExecutor:
    def __init__(self, measurements: list[CommandMeasurement]) -> None:
        self.measurements = measurements
        self.commands: list[list[str]] = []

    def discover_devices(self) -> list[GPUDeviceProfile]:
        return [GPUDeviceProfile(index=0, name="Test GPU", memory_total_mib=24576)]

    def run(
        self,
        command: Sequence[str],
        *,
        output_path: Path,
        sample_interval_seconds: float,
    ) -> CommandMeasurement:
        self.commands.append(list(command))
        assert output_path == Path("sample.mp4")
        assert sample_interval_seconds == 0.1
        return self.measurements[len(self.commands) - 1]


class FakeGPUProbe:
    def discover_devices(self) -> list[GPUDeviceProfile]:
        return [GPUDeviceProfile(index=0, name="Fake GPU", memory_total_mib=8192)]

    def memory_used_mib(self) -> list[int]:
        return [512]


def _profile(*, warmup_runs: int = 1, measured_runs: int = 2) -> LTXBenchmarkProfile:
    return LTXBenchmarkProfile(
        label="l40s-bf16",
        ltx_source="Lightricks/LTX-2@test-ref",
        pipeline="distilled",
        quantization="bf16",
        offload="none",
        width=768,
        height=1280,
        num_frames=121,
        fps=24,
        warmup_runs=warmup_runs,
        measured_runs=measured_runs,
    )


def _measurement(duration: float, memory: int, marker: str) -> CommandMeasurement:
    payload = marker.encode()
    return CommandMeasurement(
        duration_seconds=duration,
        peak_gpu_memory_mib=(memory,),
        output_size_bytes=len(payload),
        output_sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_benchmark_excludes_warmups_and_calculates_summary() -> None:
    executor = FakeExecutor(
        [
            _measurement(99.0, 1000, "warmup"),
            _measurement(10.0, 2000, "first"),
            _measurement(14.0, 2500, "second"),
        ]
    )

    report = benchmark_ltx_command(
        _profile(),
        ["python", "-m", "ltx_pipelines.distilled", "--output-path", "{output}"],
        output_path=Path("sample.mp4"),
        executor=executor,
        sample_interval_seconds=0.1,
    )

    assert len(executor.commands) == 3
    assert executor.commands[0][-1] == "sample.mp4"
    assert [sample.duration_seconds for sample in report.samples] == [10.0, 14.0]
    assert report.mean_duration_seconds == 12.0
    assert report.median_duration_seconds == 12.0
    assert report.min_duration_seconds == 10.0
    assert report.max_duration_seconds == 14.0
    assert report.mean_generated_frames_per_second == pytest.approx(121 / 12)
    assert report.max_peak_gpu_memory_mib == [2500]


def test_benchmark_redacts_secret_command_values() -> None:
    executor = FakeExecutor([_measurement(1.0, 100, "run")])

    report = benchmark_ltx_command(
        _profile(warmup_runs=0, measured_runs=1),
        [
            "ltx",
            "--hf-token",
            "secret-one",
            "--api-key=secret-two",
            "HF_TOKEN=secret-three",
            "--output-path",
            "{output}",
        ],
        output_path=Path("sample.mp4"),
        executor=executor,
        sample_interval_seconds=0.1,
    )

    assert report.command == [
        "ltx",
        "--hf-token",
        "***",
        "--api-key=***",
        "HF_TOKEN=***",
        "--output-path",
        "{output}",
    ]
    assert "secret-one" not in report.model_dump_json()
    assert "secret-two" not in report.model_dump_json()
    assert "secret-three" not in report.model_dump_json()


@pytest.mark.parametrize(
    "command",
    [
        ["ltx", "--output-path", "result.mp4"],
        ["ltx", "{output}", "--copy", "{output}"],
    ],
)
def test_benchmark_requires_exactly_one_output_placeholder(command: list[str]) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        benchmark_ltx_command(
            _profile(warmup_runs=0, measured_runs=1),
            command,
            output_path=Path("sample.mp4"),
            executor=FakeExecutor([]),
        )


def test_subprocess_executor_creates_and_hashes_output(tmp_path: Path) -> None:
    output_path = tmp_path / "artifact.bin"
    executor = SubprocessBenchmarkExecutor(gpu_probe=FakeGPUProbe())
    executor.discover_devices()
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'video')",
        str(output_path),
    ]

    measurement = executor.run(
        command,
        output_path=output_path,
        sample_interval_seconds=0.01,
    )

    assert measurement.duration_seconds > 0
    assert measurement.peak_gpu_memory_mib == (512,)
    assert measurement.output_size_bytes == 5
    assert measurement.output_sha256 == hashlib.sha256(b"video").hexdigest()


def test_subprocess_executor_removes_stale_output_before_failure(tmp_path: Path) -> None:
    output_path = tmp_path / "stale.bin"
    output_path.write_bytes(b"stale")
    executor = SubprocessBenchmarkExecutor(gpu_probe=FakeGPUProbe())
    executor.discover_devices()

    with pytest.raises(BenchmarkCommandError) as error:
        executor.run(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            output_path=output_path,
            sample_interval_seconds=0.01,
        )

    assert error.value.return_code == 7
    assert not output_path.exists()


def test_subprocess_executor_rejects_empty_output(tmp_path: Path) -> None:
    output_path = tmp_path / "empty.bin"
    executor = SubprocessBenchmarkExecutor(gpu_probe=FakeGPUProbe())
    executor.discover_devices()
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
        str(output_path),
    ]

    with pytest.raises(RuntimeError, match="empty output"):
        executor.run(
            command,
            output_path=output_path,
            sample_interval_seconds=0.01,
        )


def test_subprocess_executor_requires_device_discovery(tmp_path: Path) -> None:
    executor = SubprocessBenchmarkExecutor(gpu_probe=FakeGPUProbe())

    with pytest.raises(RuntimeError, match="discovered"):
        executor.run(
            [sys.executable, "-c", "pass"],
            output_path=tmp_path / "unused.bin",
            sample_interval_seconds=0.01,
        )


def test_benchmark_command_error_is_not_called_process_error() -> None:
    assert not issubclass(BenchmarkCommandError, subprocess.CalledProcessError)
