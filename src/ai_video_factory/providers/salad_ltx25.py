from __future__ import annotations

import json
import mimetypes
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    InferenceJobResponse,
    ObjectInput,
    ObjectOutput,
)
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.workers.ltx25 import (
    LTX_A2V_DEFAULT_PROMPT,
    LTX_A2V_GENERATION_PROFILE,
    LTX_A2V_TASK,
    ltx_a2v_application_job_id,
)

from .inference_jobs import InferenceJobExecutor, cached_inference_response


@dataclass(frozen=True, slots=True)
class LTXA2VSegmentResult:
    response: InferenceJobResponse
    video_key: str
    metadata_key: str
    metadata: dict[str, object]


def _content_type(path: Path, *, kind: str) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    return "image/png" if kind == "image" else "application/octet-stream"


def build_ltx_a2v_request(
    *,
    segment_id: str,
    avatar_image: Path,
    audio_segment: Path,
    prompt: str = LTX_A2V_DEFAULT_PROMPT,
    seed: int = 10,
    width: int = 1280,
    height: int = 720,
    fps: int = 24,
) -> tuple[InferenceJobRequest, str, str]:
    """Build a deterministic A2V job without making LTX aware of STT/timeline logic."""

    if not avatar_image.is_file():
        raise FileNotFoundError(f"avatar image does not exist: {avatar_image}")
    if not audio_segment.is_file():
        raise FileNotFoundError(f"audio segment does not exist: {audio_segment}")
    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        raise ValueError("A2V prompt must contain non-whitespace text")

    image_sha = sha256_file(avatar_image)
    audio_sha = sha256_file(audio_segment)
    job_id = ltx_a2v_application_job_id(
        segment_id=segment_id,
        prompt=normalized_prompt,
        avatar_image_sha256=image_sha,
        audio_sha256=audio_sha,
        seed=seed,
        width=width,
        height=height,
        fps=fps,
    )
    image_suffix = avatar_image.suffix.lower() or ".bin"
    audio_suffix = audio_segment.suffix.lower() or ".bin"
    image_key = f"ltx25-a2v/inputs/images/{image_sha}{image_suffix}"
    audio_key = f"ltx25-a2v/inputs/audio/{audio_sha}{audio_suffix}"
    request = InferenceJobRequest(
        job_id=job_id,
        task=LTX_A2V_TASK,
        inputs=[
            ObjectInput(
                name="avatar_image",
                key=image_key,
                sha256=image_sha,
                content_type=_content_type(avatar_image, kind="image"),
            ),
            ObjectInput(
                name="audio",
                key=audio_key,
                sha256=audio_sha,
                content_type=_content_type(audio_segment, kind="audio"),
            ),
        ],
        output=ObjectOutput(
            key=f"jobs/{job_id}/video.mp4",
            content_type="video/mp4",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=f"jobs/{job_id}/metadata.json",
                content_type="application/json",
            )
        },
        parameters={
            "generation_profile": LTX_A2V_GENERATION_PROFILE,
            "prompt": normalized_prompt,
            "seed": seed,
            "width": width,
            "height": height,
            "fps": fps,
        },
    )
    return request, image_key, audio_key


class SaladLTX25A2VProvider:
    """Application-facing provider for already-cut audio-driven avatar segments."""

    def __init__(self, *, executor: InferenceJobExecutor, temp_dir: Path) -> None:
        self._executor = executor
        self._temp_dir = temp_dir

    def generate_avatar_segment(
        self,
        *,
        segment_id: str,
        avatar_image: Path,
        audio_segment: Path,
        prompt: str = LTX_A2V_DEFAULT_PROMPT,
        seed: int = 10,
        width: int = 1280,
        height: int = 720,
        fps: int = 24,
    ) -> LTXA2VSegmentResult:
        request, image_key, audio_key = build_ltx_a2v_request(
            segment_id=segment_id,
            avatar_image=avatar_image,
            audio_segment=audio_segment,
            prompt=prompt,
            seed=seed,
            width=width,
            height=height,
            fps=fps,
        )
        image_input, audio_input = request.inputs
        assert image_input.sha256 is not None
        assert audio_input.sha256 is not None
        self._executor.ensure_input(
            avatar_image,
            key=image_key,
            sha256=image_input.sha256,
            content_type=image_input.content_type or "application/octet-stream",
            metadata={"purpose": "ltx25-a2v-avatar"},
        )
        self._executor.ensure_input(
            audio_segment,
            key=audio_key,
            sha256=audio_input.sha256,
            content_type=audio_input.content_type or "application/octet-stream",
            metadata={"purpose": "ltx25-a2v-audio-segment"},
        )
        response = cached_inference_response(self._executor.storage, request)
        if response is None:
            response = self._executor.execute(
                request,
                metadata={
                    "provider": "ltx25",
                    "capability": "audio_to_video",
                    "segment_id": segment_id,
                },
            )
        sidecars = response.sidecar_outputs or {}
        metadata_artifact = sidecars.get("metadata")
        if metadata_artifact is None:
            raise RuntimeError("LTX A2V response is missing required metadata sidecar")

        self._temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._temp_dir) as directory:
            metadata_path = Path(directory) / "metadata.json"
            stored = self._executor.storage.download(
                metadata_artifact.key,
                metadata_path,
            )
            if stored.size_bytes != metadata_artifact.size_bytes:
                raise RuntimeError("LTX A2V metadata download size mismatch")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        if not isinstance(metadata, dict):
            raise RuntimeError("LTX A2V metadata sidecar must contain a JSON object")
        return LTXA2VSegmentResult(
            response=response,
            video_key=response.output.key,
            metadata_key=metadata_artifact.key,
            metadata=metadata,
        )
