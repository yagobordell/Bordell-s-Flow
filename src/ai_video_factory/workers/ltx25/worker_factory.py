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
        a2v_backend = DirectLTX25AudioToVideoBackend(
            model_root=settings.model_root,
            device=settings.device,
            mode_controller=mode_controller,
        )
        runners.append(LTXAudioToVideoTaskRunner(
            backend=a2v_backend,
            experimental=settings.task_mode == "a2v_experimental",
        ))
        if settings.experimental_a2v_batch2_enabled:
            from .experimental_a2v_batch2 import ExperimentalA2VBatch2TaskRunner

            # Both experimental A2V tasks share the SAME backend and GPU lock.
            runners.append(ExperimentalA2VBatch2TaskRunner(backend=a2v_backend))
    return TaskRunnerRegistry(runners)
