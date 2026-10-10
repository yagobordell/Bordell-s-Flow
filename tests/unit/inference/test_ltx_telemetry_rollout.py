from pathlib import Path


def test_telemetry_rollout_requires_explicit_apply_and_stopped_workers() -> None:
    text = Path("scripts/salad/prepare_ltx_telemetry.ps1").read_text(encoding="utf-8")
    assert "[switch]$Apply" in text
    assert 'if (-not $Apply)' in text
    assert "Get-StoppedStatus" in text
    assert "pending=[Ff]alse" in text
    assert "git status --porcelain --untracked-files=no" in text
    assert "feature/ltx-time-aware-autoscaling" in text
    assert "apply_ltx_telemetry_migration.py" in text
    assert "docker buildx build --platform linux/amd64" in text
    assert "docker buildx imagetools inspect" in text
    assert "@${Digest}" in text
    assert "-Action Prepare" in text
    assert "-PinnedImage $Pinned -SkipBuild -NonInteractive" in text
    assert "-Action Start" not in text
    assert "-AllowControllerOverride" not in text
    assert "-Recreate" not in text
    assert "docker buildx build" in text.split('if (-not $Apply)')[1]


def test_rollout_checks_docker_before_mutating_postgres() -> None:
    text = Path("scripts/salad/prepare_ltx_telemetry.ps1").read_text(encoding="utf-8")
    assert text.index("& docker version") < text.index("python $Migration --apply")
    assert text.index("& docker info --format") < text.index("python $Migration --apply")
    assert text.index("& docker buildx version") < text.index("python $Migration --apply")
    assert "Docker availability (start Docker Desktop and use Linux containers)" in text
    assert "docker version *> $null" not in text
