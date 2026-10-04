"""Build task-restricted LTX runners without starting the web service."""

from __future__ import annotations

from ai_video_factory.inference.tasks import TaskRunnerRegistry

from .a2v import DirectLTX25AudioToVideoBackend, LTXAudioToVideoTaskRunner
from .model import DirectLTX25Backend, LTXPipelineModeController, LTXVideoTaskRunner
from .settings import LTX25WorkerSettings


def build_ltx25_runners(settings: LTX25WorkerSettings) -> TaskRunnerRegistry:
    # The Postgres poller selects jobs by registered task name. Do not register
    # avatar tasks in the I2V group, or I2V tasks in the avatar group.
    mode_controller = LTXPipelineModeController()
    runners = []
    if settings.task_mode in ("i2v", "both"):
        # A single backend shares the same model controller; normal single
        # requests retain their original path. Pairing is an explicit NEW task.
        backend = DirectLTX25Backend(
            model_root=settings.model_root,
            device=settings.device,
            mode_controller=mode_controller,
        )
        runners.append(LTXVideoTaskRunner(backend=backend))
        if settings.experimental_batch2_enabled:
            from .experimental_batch2 import ExperimentalBatch2TaskRunner

            runners.append(ExperimentalBatch2TaskRunner(backend=backend))
    if settings.task_mode in ("a2v", "a2v_experimental", "both"):
        runners.append(
            LTXAudioToVideoTaskRunner(
                backend=DirectLTX25AudioToVideoBackend(
                    model_root=settings.model_root,
                    device=settings.device,
                    mode_controller=mode_controller,
                ),
                experimental=settings.task_mode == "a2v_experimental",
            )
        )
    return TaskRunnerRegistry(runners)
