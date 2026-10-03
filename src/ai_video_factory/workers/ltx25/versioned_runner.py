"""Adapter mapping digest-specific queue tasks onto reviewed LTX backends."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.ports import LocalArtifact, TaskRunner


class VersionedLTXRunner:
    """Only the exact pinned Docker's alias can invoke the base GPU recipe."""

    def __init__(self, *, runner: TaskRunner, task_name: str, image: str) -> None:
        self.runner = runner
        self.task_name = task_name
        self.image = image

    def prepare(self) -> None:
        prepare = getattr(self.runner, "prepare", None)
        if prepare is not None:
            prepare()

    def ready(self) -> None:
        ready = getattr(self.runner, "ready", None)
        if ready is not None:
            ready()

    def run(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        if (
            request.task != self.task_name
            or request.parameters.get("required_worker_image") != self.image
        ):
            raise ValueError(
                "Isolated LTX runner received another Docker version's queue request"
            )
        # The routing field is deliberately outside the underlying frozen
        # LTX generation-parameter schema. The worker's original request and
        # its persistent fingerprint remain unchanged.
        parameters = {
            name: value for name, value in request.parameters.items()
            if name != "required_worker_image"
        }
        base_request = request.model_copy(update={
            "task": self.runner.task_name, "parameters": parameters,
        })
        return self.runner.run(base_request, inputs, work_dir)
