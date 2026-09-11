import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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

    assert first == second
    assert first.startswith("ideogram-keyframe-")
    assert reference.startswith("ideogram-reference-")
    assert reference != first
    assert ideogram_seed_for_job(first) == ideogram_seed_for_job(second)


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
