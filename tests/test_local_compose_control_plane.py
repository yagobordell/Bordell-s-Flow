from pathlib import Path


COMPOSE = Path("compose.yaml")
ORCHESTRATOR_DOCKERFILE = Path("docker/local/orchestrator/Dockerfile")
RENDERER_DOCKERFILE = Path("docker/local/renderer/Dockerfile")
RENDER_PHASE9 = Path("docker/local/renderer/render_phase9.sh")
LOCAL_MANAGER = Path("scripts/manage_local_stack.ps1")
DOCKERIGNORE = Path(".dockerignore")


def test_local_compose_separates_orchestrator_and_renderer() -> None:
    text = COMPOSE.read_text(encoding="utf-8")

    assert "orchestrator:" in text
    assert "renderer:" in text
    assert "docker/local/orchestrator/Dockerfile" in text
    assert "docker/local/renderer/Dockerfile" in text
    assert "source: ./data" in text
    assert "target: /workspace/data" in text
    assert "OUTPUT_DIR: /workspace/data/output" in text
    assert "TEMP_DIR: /workspace/data/tmp" in text
    assert "required: false" in text
    assert 'shm_size: "2gb"' in text


def test_orchestrator_image_stays_python_only() -> None:
    text = ORCHESTRATOR_DOCKERFILE.read_text(encoding="utf-8")

    assert text.startswith("FROM python:3.12-slim-bookworm")
    assert "COPY src ./src" in text
    assert "COPY scripts ./scripts" in text
    assert "COPY deploy ./deploy" in text
    assert "node:22" not in text
    assert "ffmpeg" not in text
    assert "cuda" not in text.lower()


def test_renderer_image_contains_remotion_chrome_and_ffmpeg() -> None:
    text = RENDERER_DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM node:22-bookworm-slim AS node-runtime" in text
    assert "FROM python:3.12-slim-bookworm" in text
    assert "ffmpeg" in text
    for dependency in (
        "libnss3",
        "libdbus-1-3",
        "libatk1.0-0",
        "libgbm-dev",
        "libasound2",
        "libxrandr2",
        "libxkbcommon-dev",
        "libxfixes3",
        "libxcomposite1",
        "libxdamage1",
        "libatk-bridge2.0-0",
        "libpango-1.0-0",
        "libcairo2",
        "libcups2",
    ):
        assert dependency in text
    assert "npx remotion browser ensure" in text
    assert "COPY remotion/src ./remotion/src" in text
    assert "COPY remotion/public ./remotion/public" in text
    assert "cuda" not in text.lower()


def test_renderer_default_runs_complete_phase9_in_order() -> None:
    text = RENDER_PHASE9.read_text(encoding="utf-8")

    compositor = text.index("run_phase9_compositor.py")
    motion = text.index("run_phase9_motion.py")
    final = text.index("run_phase9_final.py")
    assert compositor < motion < final
    assert 'CMD ["/usr/local/bin/render-phase9"]' in RENDERER_DOCKERFILE.read_text(
        encoding="utf-8"
    )


def test_local_manager_exposes_safe_one_shot_actions() -> None:
    text = LOCAL_MANAGER.read_text(encoding="utf-8")

    assert 'ValidateSet("Validate", "Build", "Smoke", "Phase9")' in text
    assert 'Invoke-Compose -Arguments @("config", "--quiet")' in text
    assert '"build"' in text
    assert '"scripts/local_container_smoke.py"' in text
    assert 'Invoke-Compose -Arguments @("run", "--rm", "renderer")' in text
    assert "manage_salad_stack" not in text


def test_generated_data_and_local_node_modules_stay_out_of_build_context() -> None:
    lines = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }

    assert "data" in lines
    assert "node_modules" in lines
    assert "remotion/node_modules" in lines
    assert ".env" in lines
