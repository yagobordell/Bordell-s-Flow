"""Build task-restricted LTX runners without starting the web service."""

from __future__ import annotations

from ai_video_factory.inference.tasks import TaskRunnerRegistry
from ai_video_factory.inference.worker_image_routes import route_for_image

from .a2v import DirectLTX25AudioToVideoBackend, LTXAudioToVideoTaskRunner
from .model import DirectLTX25Backend, LTXPipelineModeController, LTXVideoTaskRunner
from .settings import LTX25WorkerSettings
from .versioned_runner import VersionedLTXRunner


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
    if settings.task_mode in ("a2v", "both"):
        runners.append(
            LTXAudioToVideoTaskRunner(
                backend=DirectLTX25AudioToVideoBackend(
                    model_root=settings.model_root,
                    device=settings.device,
                    mode_controller=mode_controller,
                )
            )
        )
    if settings.required_worker_image:
        isolated = []
        for runner in runners:
            service = "ltx25" if runner.task_name == "video.ltx25.audio_to_video" else "ltx25_i2v"
            route = route_for_image(service, settings.required_worker_image)
            isolated.append(VersionedLTXRunner(
                runner=runner, task_name=route.task_name, image=route.image,
            ))
        runners = isolated
    return TaskRunnerRegistry(runners)
