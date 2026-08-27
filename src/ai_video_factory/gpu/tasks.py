from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

from .contracts import GPUJobRequest
from .errors import UnsupportedTaskError
from .ports import LocalArtifact, TaskRunner


class CopyTaskRunner:
    """Deterministic Phase 7 smoke task used to validate the infrastructure path."""

    task_name = "infrastructure.copy"

    def run(
        self,
        request: GPUJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        if len(inputs) != 1:
            raise ValueError("infrastructure.copy requires exactly one input")
        source = next(iter(inputs.values()))
        suffix = Path(request.output.key).suffix or ".bin"
        output = work_dir / f"output{suffix}"
        shutil.copyfile(source, output)
        return LocalArtifact(path=output, content_type=request.output.content_type)


class TaskRunnerRegistry:
    def __init__(self, runners: list[TaskRunner] | None = None) -> None:
        self._runners: dict[str, TaskRunner] = {}
        for runner in runners or []:
            self.register(runner)

    def register(self, runner: TaskRunner) -> None:
        if runner.task_name in self._runners:
            raise ValueError(f"duplicate task runner: {runner.task_name}")
        self._runners[runner.task_name] = runner

    def get(self, task_name: str) -> TaskRunner:
        try:
            return self._runners[task_name]
        except KeyError as exc:
            raise UnsupportedTaskError(f"unsupported task: {task_name}") from exc

    @classmethod
    def phase7(cls) -> TaskRunnerRegistry:
        return cls([CopyTaskRunner()])
