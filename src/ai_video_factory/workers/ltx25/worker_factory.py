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
        runners.append(
            LTXVideoTaskRunner(
                backend=DirectLTX25Backend(
                    model_root=settings.model_root,
                    device=settings.device,
                    mode_controller=mode_controller,
                )
            )
        )
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
    if settings.experimental_a2v_batch2_enabled:
        from .experimental_a2v_batch2 import ExperimentalA2VBatch2TaskRunner

        # Register on the SAME isolated backend/lock used by this A2V worker.
        # Never co-serve B2 in the production a2v or I2V service.
        if settings.task_mode != "a2v_experimental":
            raise ValueError("A2V GPU B2 requires its dedicated experimental service")
        backend = runners[-1]._backend
        runners.append(ExperimentalA2VBatch2TaskRunner(backend=backend))
    return TaskRunnerRegistry(runners)
