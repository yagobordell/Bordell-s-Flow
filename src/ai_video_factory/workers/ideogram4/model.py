from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError, NonRetryableTaskError
from ai_video_factory.inference.ports import LocalArtifact
from ai_video_factory.providers.ideogram_caption import validate_ideogram_caption

IDEOGRAM4_REFERENCE_TASK = "image.ideogram4.reference"
IDEOGRAM4_KEYFRAME_TASK = "image.ideogram4.keyframe"
IDEOGRAM4_MODEL_ID = "ideogram-ai/ideogram-4-nf4"
IDEOGRAM4_SAMPLER_PRESET = "V4_QUALITY_48"
IDEOGRAM4_GENERATION_PROFILE = "ideogram4-nf4-v4-quality-48-v4"

_SUPPORTED_TASKS = frozenset({IDEOGRAM4_REFERENCE_TASK, IDEOGRAM4_KEYFRAME_TASK})
_MAX_GENERATION_ATTEMPTS = 3
_SAFETY_SAMPLE_SIZE = (64, 64)
_CANONICAL_PREFIXES = (
    "Canonical location reference.",
    "Canonical object reference.",
    "Canonical character reference.",
)
_FALLBACK_BACKGROUND = (
    "Environment matching the high-level description with consistent geography, "
    "materials, and layout."
)
_FALLBACK_PHOTO_STYLE = {
    "aesthetics": "cinematic documentary realism",
    "lighting": "neutral natural daylight with clearly readable form",
    "photo": "realistic reference photography with natural proportions",
    "medium": "documentary photograph",
}
_FALLBACK_ART_STYLE = {
    "aesthetics": "coherent cinematic visual reference",
    "lighting": "neutral balanced lighting with clearly readable form",
    "medium": "production visual reference",
    "art_style": "clean realistic concept art",
}


class IdeogramImageParameters(BaseModel):
    """Validated controls carried by one Ideogram 4 image job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    model_id: str
    caption: str = Field(min_length=1, max_length=100_000)
    width: int = Field(ge=256, le=2048)
    height: int = Field(ge=256, le=2048)
    seed: int = Field(ge=0, le=2_147_483_647)

    @field_validator("generation_profile")
    @classmethod
    def validate_generation_profile(cls, value: str) -> str:
        if value != IDEOGRAM4_GENERATION_PROFILE:
            raise ValueError(
                "generation_profile must be exactly "
                f"{IDEOGRAM4_GENERATION_PROFILE!r}"
            )
        return value

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        if value != IDEOGRAM4_MODEL_ID:
            raise ValueError(f"model_id must be exactly {IDEOGRAM4_MODEL_ID!r}")
        return value

    @field_validator("caption")
    @classmethod
    def validate_caption(cls, value: str) -> str:
        return validate_ideogram_caption(value)

    @model_validator(mode="after")
    def validate_dimensions(self) -> Self:
        if self.width % 16 or self.height % 16:
            raise ValueError("Ideogram width and height must be divisible by 16")
        return self


class IdeogramBackend(Protocol):
    def prepare(self) -> None: ...

    def ready(self) -> None: ...

    def generate(
        self,
        *,
        parameters: IdeogramImageParameters,
        output_path: Path,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _IdeogramBindings:
    torch: Any
    pipeline_type: Any
    pipeline_config_type: Any
    presets: Mapping[str, Any]


def _load_ideogram_bindings() -> _IdeogramBindings:
    try:
        import torch
        from ideogram4 import PRESETS, Ideogram4Pipeline, Ideogram4PipelineConfig
    except ImportError as exc:
        raise RuntimeError(
            "Ideogram 4 runtime dependencies are not installed in this environment"
        ) from exc

    return _IdeogramBindings(
        torch=torch,
        pipeline_type=Ideogram4Pipeline,
        pipeline_config_type=Ideogram4PipelineConfig,
        presets=PRESETS,
    )


def ideogram_application_job_id(
    *,
    task_name: str,
    caption: str,
    width: int,
    height: int,
    model_id: str = IDEOGRAM4_MODEL_ID,
) -> str:
    if task_name not in _SUPPORTED_TASKS:
        raise ValueError(f"Unsupported Ideogram application task: {task_name}")
    purpose = "reference" if task_name == IDEOGRAM4_REFERENCE_TASK else "keyframe"
    payload = {
        "caption_sha256": hashlib.sha256(caption.encode("utf-8")).hexdigest(),
        "generation_profile": IDEOGRAM4_GENERATION_PROFILE,
        "height": height,
        "model_id": model_id,
        "purpose": purpose,
        "width": width,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"ideogram-{purpose}-{hashlib.sha256(canonical).hexdigest()[:32]}"


def ideogram_seed_for_job(job_id: str) -> int:
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _looks_like_safety_placeholder(image: Image.Image) -> bool:
    """Detect Ideogram's documented gray safety-filter placeholder without OCR."""

    sample = image.convert("RGB").resize(_SAFETY_SAMPLE_SIZE)
    pixels = list(sample.getdata())
    if not pixels:
        return False

    neutral_midgray = 0
    bright_neutral = 0
    dark = 0
    for red, green, blue in pixels:
        low = min(red, green, blue)
        high = max(red, green, blue)
        luminance = (red + green + blue) / 3
        chroma = high - low
        if 125 <= luminance <= 145 and chroma <= 10:
            neutral_midgray += 1
        if luminance >= 180 and chroma <= 20:
            bright_neutral += 1
        if luminance < 100:
            dark += 1

    total = len(pixels)
    midgray_fraction = neutral_midgray / total
    bright_fraction = bright_neutral / total
    dark_fraction = dark / total
    return (
        midgray_fraction >= 0.90
        and 0.001 <= bright_fraction <= 0.05
        and dark_fraction <= 0.01
    )


def _compact_background(caption: dict[str, Any]) -> str:
    composition = caption["compositional_deconstruction"]
    background = str(composition["background"]).strip()
    high_level = str(caption.get("high_level_description", "")).strip()
    if high_level and background.startswith(high_level):
        background = background[len(high_level) :].lstrip(" .")
    return background or _FALLBACK_BACKGROUND


def _concise_high_level(caption: dict[str, Any]) -> str:
    high_level = str(caption.get("high_level_description", "")).strip()
    original = high_level
    for prefix in _CANONICAL_PREFIXES:
        if high_level.startswith(prefix):
            high_level = high_level[len(prefix) :].strip()
            break
    if not high_level:
        return original

    first_sentence, separator, _ = high_level.partition(". ")
    concise = first_sentence.strip()
    if separator and concise and not concise.endswith((".", "!", "?")):
        concise += "."
    return concise or original


def _fallback_style(style: Mapping[str, Any]) -> dict[str, Any]:
    fallback = (
        dict(_FALLBACK_PHOTO_STYLE)
        if "photo" in style
        else dict(_FALLBACK_ART_STYLE)
    )
    palette = style.get("color_palette")
    if isinstance(palette, list) and palette:
        fallback["color_palette"] = palette
    return fallback


def _caption_variant(
    caption: str,
    *,
    simplify_style: bool,
    concise_high_level: bool = False,
) -> str:
    payload = json.loads(caption)
    if not isinstance(payload, dict):
        return caption
    composition = payload.get("compositional_deconstruction")
    style = payload.get("style_description")
    if not isinstance(composition, dict) or not isinstance(style, dict):
        return caption

    high_level = (
        _concise_high_level(payload)
        if concise_high_level
        else payload.get("high_level_description", "")
    )
    variant = {
        "high_level_description": high_level,
        "style_description": _fallback_style(style) if simplify_style else style,
        "compositional_deconstruction": {
            "background": (
                _FALLBACK_BACKGROUND
                if simplify_style
                else _compact_background(payload)
            ),
            "elements": composition.get("elements", []),
        },
    }
    rendered = json.dumps(variant, ensure_ascii=False, separators=(",", ":"))
    return validate_ideogram_caption(rendered)


def _generation_attempts(caption: str, seed: int) -> list[tuple[str, int]]:
    variants = [
        caption,
        _caption_variant(caption, simplify_style=False),
        _caption_variant(
            caption,
            simplify_style=True,
            concise_high_level=True,
        ),
    ]
    attempts: list[tuple[str, int]] = []
    seen: set[str] = set()
    for variant in variants:
        if variant in seen:
            continue
        seen.add(variant)
        attempt_seed = (seed + len(attempts)) & 0x7FFFFFFF
        attempts.append((variant, attempt_seed))
        if len(attempts) == _MAX_GENERATION_ATTEMPTS:
            return attempts

    fallback_caption = attempts[-1][0] if attempts else caption
    while len(attempts) < _MAX_GENERATION_ATTEMPTS:
        attempt_seed = (seed + len(attempts)) & 0x7FFFFFFF
        attempts.append((fallback_caption, attempt_seed))
    return attempts


class Ideogram4Backend:
    """Resident Ideogram 4 NF4 runtime using the V4_QUALITY_48 sampler."""

    def __init__(
        self,
        *,
        model_root: Path,
        model_repository: str,
        model_revision: str,
        device: str = "cuda",
        sampler_preset: str = IDEOGRAM4_SAMPLER_PRESET,
    ) -> None:
        self._model_root = model_root
        self._model_repository = model_repository
        self._model_revision = model_revision
        self._device = device
        self._sampler_preset = sampler_preset
        self._bindings: _IdeogramBindings | None = None
        self._pipeline: Any | None = None
        self._lock = threading.Lock()

    @property
    def runtime_loaded(self) -> bool:
        return self._pipeline is not None

    @property
    def bootstrap_marker(self) -> Path:
        return self._model_root / ".ready"

    def prepare(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            self._get_or_build_pipeline(bindings)

    def ready(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            if self._pipeline is None:
                raise RuntimeError("Ideogram 4 runtime has not been prepared")

    def generate(
        self,
        *,
        parameters: IdeogramImageParameters,
        output_path: Path,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        with self._lock:
            bindings = self._get_bindings()
            pipeline = self._get_or_build_pipeline(bindings)
            preset = bindings.presets[self._sampler_preset]
            for caption, seed in _generation_attempts(parameters.caption, parameters.seed):
                images = pipeline(
                    caption,
                    height=parameters.height,
                    width=parameters.width,
                    num_steps=preset.num_steps,
                    guidance_schedule=preset.guidance_schedule,
                    mu=preset.mu,
                    std=preset.std,
                    seed=seed,
                    raise_on_caption_issues=True,
                )
                if len(images) != 1:
                    raise RuntimeError("Ideogram 4 did not return exactly one image")
                image = images[0]
                if image.size != (parameters.width, parameters.height):
                    raise RuntimeError("Ideogram 4 returned an image with unexpected dimensions")
                if _looks_like_safety_placeholder(image):
                    continue
                image.save(output_path, format="PNG")
                return

        raise NonRetryableTaskError(
            "Ideogram 4 safety filter blocked all deterministic caption variants"
        )

    def _get_bindings(self) -> _IdeogramBindings:
        if self._bindings is None:
            self._bindings = _load_ideogram_bindings()
        return self._bindings

    def _validate_bootstrap(self) -> None:
        if not self.bootstrap_marker.is_file():
            raise ModelBootstrapPendingError(
                f"Ideogram model bootstrap marker is missing: {self.bootstrap_marker}"
            )
        marker = self.bootstrap_marker.read_text(encoding="utf-8").strip()
        expected = f"{self._model_repository}@{self._model_revision}"
        if marker != expected:
            raise RuntimeError(
                f"Ideogram bootstrap marker {marker!r} does not match {expected!r}"
            )

    def _validate_runtime(self, bindings: _IdeogramBindings) -> None:
        if self._device.startswith("cuda") and not bindings.torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the Ideogram 4 NF4 runtime")
        if self._sampler_preset not in bindings.presets:
            raise RuntimeError(f"Ideogram sampler preset is unavailable: {self._sampler_preset}")

    def _get_or_build_pipeline(self, bindings: _IdeogramBindings) -> Any:
        if self._pipeline is not None:
            return self._pipeline

        dtype = bindings.torch.bfloat16
        config = bindings.pipeline_config_type(weights_repo=self._model_repository)
        self._pipeline = bindings.pipeline_type.from_pretrained(
            config=config,
            device=self._device,
            dtype=dtype,
        )
        return self._pipeline


class IdeogramImageTaskRunner:
    def __init__(self, *, backend: IdeogramBackend, task_name: str) -> None:
        if task_name not in _SUPPORTED_TASKS:
            raise ValueError(f"Unsupported Ideogram task runner: {task_name}")
        self._backend = backend
        self.task_name = task_name

    def prepare(self) -> None:
        self._backend.prepare()

    def ready(self) -> None:
        self._backend.ready()

    def run(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        self._validate_request(request, inputs)
        parameters = IdeogramImageParameters.model_validate(request.parameters)
        output = work_dir / "image.png"
        self._backend.generate(parameters=parameters, output_path=output)
        if not output.is_file() or output.stat().st_size <= 8:
            raise RuntimeError("Ideogram 4 produced no usable PNG output")
        return LocalArtifact(path=output, content_type="image/png")

    def _validate_request(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
    ) -> None:
        if request.task != self.task_name:
            raise ValueError(
                f"IdeogramImageTaskRunner cannot execute task {request.task!r}"
            )
        if inputs or request.inputs:
            raise ValueError("Ideogram 4 open-weight tasks do not accept object inputs")
        if request.output.content_type != "image/png":
            raise ValueError("Ideogram 4 output must be image/png")
        if Path(request.output.key).suffix.lower() != ".png":
            raise ValueError("Ideogram 4 output key must end in .png")
