import json
from pathlib import Path

from ai_video_factory.workers.flux2_klein.model import (
    FLUX2_KLEIN_MODEL_ID,
    FLUX2_KLEIN_MODEL_REVISION,
    Flux2KleinBackend,
)


def test_flux2_klein_salad_manifest_contract() -> None:
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = document["services"]["flux2_klein"]

    assert service["group_name"] == "ai-video-factory-flux2-klein-worker"
    assert service["queue_name"] == "ai-video-factory-flux2-klein-jobs"
    assert service["dockerfile"] == "docker/workers/flux2-klein/Dockerfile"
    assert service["image"].endswith(":flux2-klein-4b-bf16-v1")
    assert service["resources"] == {
        "cpu": 8,
        "memory": 32768,
        "shm_size": 8192,
        "storage_amount": 103079215104,
        "gpu_class_names": ["RTX 4090 (24 GB)"],
    }
    assert service["autoscaler"]["min_replicas"] == 0
    assert service["autoscaler"]["max_replicas"] == 1
    assert service["environment"]["FLUX2_KLEIN_MODEL_REPOSITORY"] == FLUX2_KLEIN_MODEL_ID
    assert service["environment"]["FLUX2_KLEIN_MODEL_REVISION"] == FLUX2_KLEIN_MODEL_REVISION
    assert service["environment"]["FLUX2_KLEIN_DOWNLOAD_HARD_TIMEOUT_SECONDS"] == "2400"
    assert service["environment"]["FLUX2_KLEIN_READY_TIMEOUT_SECONDS"] == "1200"


def test_flux2_klein_docker_dependencies_are_resolver_compatible() -> None:
    dockerfile = Path("docker/workers/flux2-klein/Dockerfile").read_text(encoding="utf-8")

    assert "'diffusers==0.40.0'" in dockerfile
    assert "'transformers==5.16.1'" in dockerfile
    assert "'huggingface-hub==1.32.0'" in dockerfile
    assert "'transformers>=4.57,<5'" not in dockerfile
    assert "'huggingface-hub[cli]>=0.36,<1'" not in dockerfile


def test_flux2_klein_health_and_readiness_are_separate() -> None:
    entrypoint = Path("docker/workers/flux2-klein/entrypoint.sh").read_text(encoding="utf-8")

    assert "wait_for_health" in entrypoint
    assert "download-models" in entrypoint
    assert "wait_for_ready" in entrypoint
    health_call = entrypoint.index("if ! wait_for_health")
    download_call = entrypoint.index("\ndownload-models\n")
    ready_call = entrypoint.index("if ! wait_for_ready")
    assert health_call < download_call < ready_call
    assert "request_reallocation" in entrypoint


def test_flux2_klein_backend_requires_exact_bootstrap_identity(tmp_path: Path) -> None:
    backend = Flux2KleinBackend(model_root=tmp_path, device="cuda")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (tmp_path / ".ready").write_text("wrong@revision\n", encoding="utf-8")

    try:
        backend.ready()
    except Exception as exc:
        assert "marker does not match" in str(exc)
    else:
        raise AssertionError("readiness accepted a stale model marker")


def test_phase4_and_phase6_use_flux2_klein_fallback_contract() -> None:
    phase4 = Path("scripts/run_phase4_assets.py").read_text(encoding="utf-8")
    phase6 = Path("scripts/run_phase6_keyframes.py").read_text(encoding="utf-8")

    for script in (phase4, phase6):
        assert "SaladFlux2KleinImageProvider" in script
        assert "flux2_klein" in script

    assert "FLUX2_KLEIN_REFERENCE_TASK" in phase4
    assert "FLUX2_KLEIN_KEYFRAME_TASK" in phase6


def test_phase4_and_phase6_default_to_flux_without_removing_ideogram() -> None:
    phase4 = Path("scripts/run_phase4_assets.py").read_text(encoding="utf-8")
    phase6 = Path("scripts/run_phase6_keyframes.py").read_text(encoding="utf-8")

    for script in (phase4, phase6):
        assert 'default="flux2_klein"' in script
        assert 'choices=("flux2_klein", "ideogram4")' in script
        assert "SafetyFallbackImageProvider" in script
        assert "SaladIdeogramImageProvider" in script


def test_no_operational_flux1_schnell_references_remain() -> None:
    roots = (
        Path("src"),
        Path("scripts"),
        Path("deploy"),
        Path("docker"),
    )
    forbidden = (
        "flux_" + "schnell",
        "flux-" + "schnell",
        "FLUX.1-" + "schnell",
        "flux1_" + "schnell",
    )
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if any(token in text for token in forbidden):
                offenders.append(str(path))

    assert offenders == []

    legacy_operational_files = (
        Path("src/ai_video_factory/workers/flux_schnell/__init__.py"),
        Path("src/ai_video_factory/workers/flux_schnell/model.py"),
        Path("src/ai_video_factory/workers/flux_schnell/runtime.py"),
        Path("src/ai_video_factory/workers/flux_schnell/settings.py"),
        Path("docker/workers/flux-schnell/Dockerfile"),
        Path("docker/workers/flux-schnell/download_models.sh"),
        Path("docker/workers/flux-schnell/entrypoint.sh"),
    )
    assert [str(path) for path in legacy_operational_files if path.is_file()] == []
