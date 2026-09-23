from pathlib import Path

WORKER_DOCKERFILES = tuple(sorted(Path("docker/workers").glob("*/Dockerfile")))
WORKER_ENTRYPOINTS = tuple(sorted(Path("docker/workers").glob("*/entrypoint.sh")))


def test_worker_images_keep_basic_supply_chain_and_runtime_hygiene() -> None:
    assert WORKER_DOCKERFILES

    for dockerfile in WORKER_DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")
        assert "ARG SALAD_WORKER_SHA256=" in text, dockerfile
        assert "sha256sum --check --strict" in text, dockerfile
        assert "rm -f /tmp/salad-worker.tar.gz" in text, dockerfile
        assert "rm -rf /var/lib/apt/lists/*" in text, dockerfile
        assert "USER worker" in text, dockerfile


def test_worker_entrypoints_fail_if_http_health_never_starts() -> None:
    assert WORKER_ENTRYPOINTS

    for entrypoint in WORKER_ENTRYPOINTS:
        text = entrypoint.read_text(encoding="utf-8")
        assert "wait_for_health()" in text, entrypoint
        assert "if ! wait_for_health; then" in text, entrypoint
        assert "exit 1" in text, entrypoint
