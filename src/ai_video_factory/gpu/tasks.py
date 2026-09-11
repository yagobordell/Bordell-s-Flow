"""Phase-specific compatibility helpers over the neutral inference task registry."""

from ai_video_factory.inference.ports import TaskRunner
from ai_video_factory.inference.tasks import CopyTaskRunner
from ai_video_factory.inference.tasks import TaskRunnerRegistry as _InferenceTaskRunnerRegistry


class TaskRunnerRegistry(_InferenceTaskRunnerRegistry):
    @classmethod
    def phase7(cls) -> "TaskRunnerRegistry":
        return cls([CopyTaskRunner()])

    @classmethod
    def phase8(cls, video_runner: TaskRunner) -> "TaskRunnerRegistry":
        return cls([CopyTaskRunner(), video_runner])


__all__ = ["CopyTaskRunner", "TaskRunnerRegistry"]
