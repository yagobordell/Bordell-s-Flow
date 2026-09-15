import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, ImageDraw

import ai_video_factory.workers.ideogram4.model as ideogram_model
from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.providers.ideogram_caption import (
    IdeogramCaptionPlan,
    IdeogramElementPlan,
    IdeogramStylePlan,
    render_ideogram_caption,
    validate_ideogram_caption,
)
from ai_video_factory.workers.ideogram4 import (
    IDEOGRAM4_GENERATION_PROFILE,
    IDEOGRAM4_KEYFRAME_TASK,
    IDEOGRAM4_MODEL_ID,
    IDEOGRAM4_REFERENCE_TASK,
    Ideogram4Backend,
    Ideogram4WorkerSettings,
    IdeogramImageParameters,
    IdeogramImageTaskRunner,
    ideogram_application_job_id,
    ideogram_seed_for_job,
)
from ai_video_factory.workers.ideogram4.model import (
    _generation_attempts,
    _looks_like_safety_placeholder,
)


def _caption() -> str:
    return render_ideogram_caption(
        IdeogramCaptionPlan(
            high_level_description="A samurai stands in a mountain stronghold.",
            style=IdeogramStylePlan(
                aesthetics="cinematic documentary realism",
                lighting="soft directional daylight",
                medium="documentary photograph",
                render_mode="photo",
                render_description="35mm cinematic photography, realistic materials",
                color_palette=["#2f3a32", "#c5a46d"],
            ),
            background="Weathered timber walls and distant forested mountains.",
            elements=[
                IdeogramElementPlan(
                    description="A historically grounded samurai in dark lamellar armor.",
                    bbox=[120, 260, 920, 760],
                )
            ],
        )
    )


def _request(task_name: str = IDEOGRAM4_KEYFRAME_TASK) -> InferenceJobRequest:
    caption = _caption()
    job_id = ideogram_application_job_id(
        task_name=task_name,
        caption=caption,
        width=1024,
        height=1536,
    )
    return InferenceJobRequest(
        job_id=job_id,
        task=task_name,
        output=ObjectOutput(
            key=f"jobs/{job_id}/image.png",
            content_type="image/png",
        ),
        parameters={
            "generation_profile": IDEOGRAM4_GENERATION_PROFILE,
            "model_id": IDEOGRAM4_MODEL_ID,
            "caption": caption,
            "width": 1024,
            "height": 1536,
            "seed": ideogram_seed_for_job(job_id),
        },
    )


def _blocked_placeholder(size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGB", size, (132, 133, 134))
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.rectangle(
        (
            round(width * 0.20),
            round(height * 0.49),
            round(width * 0.80),
            round(height * 0.51),
        ),
        fill=(225, 225, 225),
    )
    return image


def test_ideogram_runtime_imports_in_fresh_interpreter() -> None:
    environment = os.environ.copy()
    environment["INFERENCE_WORKER_MODE"] = "local"
    completed = subprocess.run(
        [sys.executable, "-c", "import ai_video_factory.workers.ideogram4.runtime"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr


def test_ideogram_caption_renderer_preserves_official_key_order() -> None:
    caption = _caption()
    parsed = json.loads(caption)

    assert list(parsed) == [
        "high_level_description",
        "style_description",
        "compositional_deconstruction",
    ]
    assert list(parsed["style_description"]) == [
        "aesthetics",
        "lighting",
        "photo",
        "medium",
        "color_palette",
    ]
    assert parsed["style_description"]["color_palette"] == ["#2F3A32", "#C5A46D"]
    assert list(parsed["compositional_deconstruction"]) == ["background", "elements"]
    assert list(parsed["compositional_deconstruction"]["elements"][0]) == [
        "type",
        "bbox",
        "desc",
    ]
    assert validate_ideogram_caption(caption) == caption


def test_ideogram_caption_contract_rejects_plain_text_and_text_elements() -> None:
    with pytest.raises(ValueError, match="structured JSON"):
        validate_ideogram_caption("plain prompt")

    parsed = json.loads(_caption())
    parsed["compositional_deconstruction"]["elements"][0]["type"] = "text"
    with pytest.raises(ValueError, match="only object elements"):
        validate_ideogram_caption(json.dumps(parsed))


def test_ideogram_application_job_id_and_seed_are_deterministic() -> None:
    caption = _caption()
    first = ideogram_application_job_id(
        task_name=IDEOGRAM4_KEYFRAME_TASK,
        caption=caption,
        width=1024,
        height=1536,
    )
    second = ideogram_application_job_id(
        task_name=IDEOGRAM4_KEYFRAME_TASK,
        caption=caption,
        width=1024,
        height=1536,
    )
    reference = ideogram_application_job_id(
        task_name=IDEOGRAM4_REFERENCE_TASK,
        caption=caption,
        width=1024,
        height=1536,
    )

    assert IDEOGRAM4_GENERATION_PROFILE.endswith("-v3")
    assert first == second
    assert first.startswith("ideogram-keyframe-")
    assert reference.startswith("ideogram-reference-")
    assert reference != first
    assert ideogram_seed_for_job(first) == ideogram_seed_for_job(second)


def test_ideogram_safety_placeholder_detector_is_conservative() -> None:
    blocked = _blocked_placeholder((1024, 1024))
    normal = Image.new("RGB", (1024, 1024), (40, 80, 120))
    draw = ImageDraw.Draw(normal)
    draw.rectangle((0, 512, 1024, 1024), fill=(210, 170, 120))

    assert _looks_like_safety_placeholder(blocked) is True
    assert _looks_like_safety_placeholder(normal) is False


def test_ideogram_generation_attempts_reduce_redundancy_without_changing_subject() -> None:
    payload = json.loads(_caption())
    high_level = payload["high_level_description"]
    payload["compositional_deconstruction"]["background"] = (
        f"{high_level} One coherent reusable environment with stable materials and layout."
    )
    caption = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    attempts = _generation_attempts(caption, 41)

    assert len(attempts) == 3
    assert [seed for _, seed in attempts] == [41, 42, 43]
    assert len({attempt_caption for attempt_caption, _ in attempts}) == 3
    for attempt_caption, _ in attempts:
        assert validate_ideogram_caption(attempt_caption) == attempt_caption
        assert json.loads(attempt_caption)["high_level_description"] == high_level
    compact = json.loads(attempts[1][0])
    simplified = json.loads(attempts[2][0])
    assert compact["compositional_deconstruction"]["background"] == (
        "One coherent reusable environment with stable materials and layout."
    )
    assert simplified["style_description"]["photo"].startswith("realistic reference")


class FakeBackend:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.ready_calls = 0
        self.calls: list[IdeogramImageParameters] = []

    def prepare(self) -> None:
        self.prepare_calls += 1

    def ready(self) -> None:
        self.ready_calls += 1

    def generate(self, *, parameters: IdeogramImageParameters, output_path: Path) -> None:
        self.calls.append(parameters)
        output_path.write_bytes(b"\x89PNG\r\n\x1a\nideogram")


def test_ideogram_task_runner_writes_one_png(tmp_path: Path) -> None:
    backend = FakeBackend()
    runner = IdeogramImageTaskRunner(
        backend=backend,
        task_name=IDEOGRAM4_KEYFRAME_TASK,
    )
    artifact = runner.run(_request(), {}, tmp_path)

    assert artifact.content_type == "image/png"
    assert artifact.path.read_bytes().startswith(b"\x89PNG")
    assert len(backend.calls) == 1
    assert backend.calls[0].width == 1024
    assert backend.calls[0].height == 1536


def test_ideogram_backend_builds_once_and_uses_quality_preset(tmp_path: Path, monkeypatch) -> None:
    model_root = tmp_path / "ideogram4"
    model_root.mkdir()
    (model_root / ".ready").write_text(f"{IDEOGRAM4_MODEL_ID}@main\n", encoding="utf-8")
    state: dict[str, Any] = {"builds": 0, "calls": []}

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

    class FakeTorch:
        cuda = FakeCuda()
        bfloat16 = "bfloat16"

    class FakeImage:
        size = (1024, 1536)

        def convert(self, mode: str) -> Image.Image:
            assert mode == "RGB"
            return Image.new("RGB", self.size, (50, 100, 150))

        def save(self, path: Path, *, format: str) -> None:
            state["saved_format"] = format
            path.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    class FakePipeline:
        def __call__(self, caption: str, **kwargs: Any) -> list[FakeImage]:
            state["calls"].append((caption, kwargs))
            return [FakeImage()]

    class FakePipelineType:
        @classmethod
        def from_pretrained(cls, **kwargs: Any) -> FakePipeline:
            state["builds"] += 1
            state["build_kwargs"] = kwargs
            return FakePipeline()

    class FakePipelineConfig:
        def __init__(self, *, weights_repo: str) -> None:
            self.weights_repo = weights_repo

    monkeypatch.setattr(
        ideogram_model,
        "_load_ideogram_bindings",
        lambda: ideogram_model._IdeogramBindings(
            torch=FakeTorch,
            pipeline_type=FakePipelineType,
            pipeline_config_type=FakePipelineConfig,
            presets={
                "V4_QUALITY_48": SimpleNamespace(
                    num_steps=48,
                    guidance_schedule=(3.0,) * 3 + (7.0,) * 45,
                    mu=0.0,
                    std=1.5,
                )
            },
        ),
    )

    backend = Ideogram4Backend(
        model_root=model_root,
        model_repository=IDEOGRAM4_MODEL_ID,
        model_revision="main",
    )
    backend.prepare()
    backend.prepare()
    output = tmp_path / "output.png"
    backend.generate(
        parameters=IdeogramImageParameters.model_validate(_request().parameters),
        output_path=output,
    )

    assert state["builds"] == 1
    assert state["build_kwargs"]["device"] == "cuda"
    assert state["build_kwargs"]["dtype"] == "bfloat16"
    assert state["build_kwargs"]["config"].weights_repo == IDEOGRAM4_MODEL_ID
    assert state["calls"][0][1]["num_steps"] == 48
    assert state["calls"][0][1]["raise_on_caption_issues"] is True
    assert state["saved_format"] == "PNG"


def test_ideogram_backend_retries_blocked_output_with_caption_fallbacks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    model_root = tmp_path / "ideogram4"
    model_root.mkdir()
    (model_root / ".ready").write_text(f"{IDEOGRAM4_MODEL_ID}@main\n", encoding="utf-8")
    calls: list[tuple[str, int]] = []

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

    class FakeTorch:
        cuda = FakeCuda()
        bfloat16 = "bfloat16"

    class FakePipeline:
        def __call__(self, caption: str, **kwargs: Any) -> list[Image.Image]:
            calls.append((caption, kwargs["seed"]))
            if len(calls) < 3:
                return [_blocked_placeholder((1024, 1536))]
            return [Image.new("RGB", (1024, 1536), (30, 90, 150))]

    class FakePipelineType:
        @classmethod
        def from_pretrained(cls, **kwargs: Any) -> FakePipeline:
            return FakePipeline()

    class FakePipelineConfig:
        def __init__(self, *, weights_repo: str) -> None:
            self.weights_repo = weights_repo

    monkeypatch.setattr(
        ideogram_model,
        "_load_ideogram_bindings",
        lambda: ideogram_model._IdeogramBindings(
            torch=FakeTorch,
            pipeline_type=FakePipelineType,
            pipeline_config_type=FakePipelineConfig,
            presets={
                "V4_QUALITY_48": SimpleNamespace(
                    num_steps=48,
                    guidance_schedule=(3.0,) * 3 + (7.0,) * 45,
                    mu=0.0,
                    std=1.5,
                )
            },
        ),
    )

    backend = Ideogram4Backend(
        model_root=model_root,
        model_repository=IDEOGRAM4_MODEL_ID,
        model_revision="main",
    )
    backend.prepare()
    parameters = IdeogramImageParameters.model_validate(_request().parameters)
    output = tmp_path / "output.png"
    backend.generate(parameters=parameters, output_path=output)

    assert [seed for _, seed in calls] == [
        parameters.seed,
        (parameters.seed + 1) & 0x7FFFFFFF,
        (parameters.seed + 2) & 0x7FFFFFFF,
    ]
    assert len({caption for caption, _ in calls}) >= 2
    expected_high_level = json.loads(parameters.caption)["high_level_description"]
    attempt_high_levels = [
        json.loads(caption)["high_level_description"] for caption, _ in calls
    ]
    assert attempt_high_levels == [expected_high_level] * 3
    assert output.is_file()
    assert not _looks_like_safety_placeholder(Image.open(output))


def test_ideogram_worker_settings_and_salad_manifest() -> None:
    settings = Ideogram4WorkerSettings(
        _env_file=None,
        worker_mode="local",
        worker_lease_seconds=60,
        worker_heartbeat_seconds=10,
    )
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = document["services"]["ideogram4"]

    assert settings.model_repository == IDEOGRAM4_MODEL_ID
    assert settings.sampler_preset == "V4_QUALITY_48"
    assert service["queue_name"] == "ai-video-factory-ideogram4-jobs"
    assert service["resources"]["gpu_class_names"] == ["RTX 4090 (24 GB)"]
    assert service["autoscaler"]["min_replicas"] == 0
    assert service["autoscaler"]["max_replicas"] == 4
    assert service["required_environment"] == ["HF_TOKEN"]


def test_ideogram_container_pins_official_runtime_and_stays_model_specific() -> None:
    dockerfile = Path("docker/workers/ideogram4/Dockerfile").read_text(encoding="utf-8")
    entrypoint = Path("docker/workers/ideogram4/entrypoint.sh").read_text(encoding="utf-8")
    downloader = Path("docker/workers/ideogram4/download_models.sh").read_text(encoding="utf-8")

    assert "990fe1c4e950bb9e9dc90e01c0ad98ba434f83c2" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "COPY src /opt/factory/src" in dockerfile
    assert "COPY . /opt/factory" not in dockerfile
    assert "ai_video_factory.workers.ideogram4.runtime:app" in entrypoint
    assert "HF_HUB_OFFLINE=0 hf download" in downloader