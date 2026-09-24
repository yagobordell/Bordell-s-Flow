from pathlib import Path

WORKER_DOCKERFILES = tuple(sorted(Path("docker/workers").glob("*/Dockerfile")))
WORKER_ENTRYPOINTS = tuple(
    path
    for path in sorted(Path("docker/workers").glob("*/entrypoint.sh"))
    if path.parent.name != "common"
)
COMMON_ENTRYPOINT = Path("docker/workers/common/entrypoint.sh")
SPECIAL_ENTRYPOINTS = {"fish-speech", "ideogram4"}


def test_worker_images_keep_basic_supply_chain_and_runtime_hygiene() -> None:
    assert WORKER_DOCKERFILES

    for dockerfile in WORKER_DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")
        assert "ARG SALAD_WORKER_SHA256=" in text, dockerfile
        assert "sha256sum --check --strict" in text, dockerfile
        assert "rm -f /tmp/salad-worker.tar.gz" in text, dockerfile
        assert "rm -rf /var/lib/apt/lists/*" in text, dockerfile
        assert "USER worker" in text, dockerfile


def test_standard_worker_entrypoints_share_postgres_polling_lifecycle() -> None:
    common = COMMON_ENTRYPOINT.read_text(encoding="utf-8")
    assert "wait_for_endpoint()" in common
    assert "if ! wait_for_endpoint /health 120 2; then" in common
    assert "wait_for_endpoint /ready" in common
    assert "polling canonical Postgres jobs" in common
    assert "SALAD_QUEUE_ENABLED" not in common
    assert "salad-http-job-queue-worker" not in common

    for entrypoint in WORKER_ENTRYPOINTS:
        text = entrypoint.read_text(encoding="utf-8")
        if entrypoint.parent.name in SPECIAL_ENTRYPOINTS:
            assert "wait_for_health()" in text, entrypoint
            assert "if ! wait_for_health; then" in text, entrypoint
        else:
            assert "exec /usr/local/bin/common-worker-entrypoint" in text, entrypoint
