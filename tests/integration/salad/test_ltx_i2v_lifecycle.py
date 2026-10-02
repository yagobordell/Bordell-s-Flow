"""Offline PowerShell lifecycle checks: never contact Salad, Postgres or R2."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "scripts" / "smoke" / "run_ltx25_i2v_smoke_controlled.ps1"
PWSH = shutil.which("pwsh")
PINNED = "docker.io/yagobordell/ai-video-factory@sha256:" + "a" * 64

pytestmark = pytest.mark.skipif(PWSH is None, reason="PowerShell 7 is not installed")


def _fake_checkout(tmp_path: Path, fail_at: str | None) -> tuple[Path, Path, Path]:
    repo = tmp_path / "fake-repo"
    smoke = repo / "scripts" / "smoke"
    salad = repo / "scripts" / "salad"
    pipeline = repo / "scripts" / "pipeline"
    deploy = repo / "deploy" / "salad"
    for directory in (smoke, salad, pipeline, deploy):
        directory.mkdir(parents=True)
    (smoke / WRAPPER.name).write_text(WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
    manifest = {
        "services": {
            "ltx25": {
                "group_name": "avatar-group",
                "environment": {"LTX_WORKER_TASK_MODE": "a2v"},
            },
            "ltx25_i2v": {
                "group_name": "i2v-group",
                "image": "docker.io/yagobordell/ai-video-factory:ltx25-isolated-tasks-v11",
                "environment": {"LTX_WORKER_TASK_MODE": "i2v"},
            },
        }
    }
    (deploy / "services.json").write_text(json.dumps(manifest), encoding="utf-8")
    (salad / "_env_file.ps1").write_text(
        "function Import-EnvFile { param([string]$Path) }\n", encoding="utf-8"
    )
    avatar_status = "running" if fail_at == "avatar" else "stopped"
    status_writer = "Write-Host" if fail_at == "status_host" else "Write-Output"
    manager = (
        "[CmdletBinding()]\n"
        "param([string]$Action,[string]$Service,[int]$Replicas,[string]$EnvFile,"
        "[switch]$AllowBootstrappingInstance,[switch]$NonInteractive)\n"
        "if ($Action -eq 'Status') {\n"
        "  if ($Service -eq 'ltx25') {\n"
        f"    {status_writer} 'service=ltx25 status={avatar_status} replicas=0'; exit 0\n"
        "  }\n"
        f"  {status_writer} 'service=ltx25_i2v status=stopped image={PINNED}'; exit 0\n"
        "}\n"
        "Add-Content -LiteralPath (Join-Path $PSScriptRoot 'events.txt') "
        '-Value "$Service $Action"\n'
    )
    (salad / "manage_salad_worker.ps1").write_text(manager, encoding="utf-8")
    (smoke / "check_ltx25_python_source.py").write_text(
        "print('fake source')\n", encoding="utf-8"
    )
    (pipeline / "check_r2_ready.py").write_text("print('fake R2')\n", encoding="utf-8")
    (smoke / "ltx25_i2v_smoke.py").write_text("print('fake smoke')\n", encoding="utf-8")
    (smoke / "wait_salad_ltx25_ready.py").write_text(
        "import sys\n"
        "config_only = '--check-config-only' in sys.argv\n"
        "print('fake ready')\n"
        "sys.exit(int((config_only and "
        + ("True" if fail_at == "config" else "False")
        + ") or (not config_only and "
        + ("True" if fail_at == "ready" else "False")
        + ")))\n",
        encoding="utf-8",
    )
    (pipeline / "run_phase8_videos.py").write_text(
        "import sys\nprint('fake runner')\nsys.exit("
        + ("1" if fail_at == "runner" else "0")
        + ")\n",
        encoding="utf-8",
    )
    image = repo / "image.png"
    image.write_bytes(b"fake image; smoke preparation is intentionally mocked")
    return smoke / WRAPPER.name, image, salad / "events.txt"


@pytest.mark.parametrize(
    ("fail_at", "should_pass", "expected_events"),
    [
        (None, True, ["ltx25_i2v Start", "ltx25_i2v Stop"]),
        ("ready", False, ["ltx25_i2v Start", "ltx25_i2v Stop"]),
        ("config", False, []),
        ("runner", False, ["ltx25_i2v Start", "ltx25_i2v Stop"]),
        ("avatar", False, []),
        ("status_host", False, []),
    ],
)
def test_isolated_i2v_never_operates_avatar_group_and_cleans_up(
    tmp_path: Path,
    fail_at: str | None,
    should_pass: bool,
    expected_events: list[str],
) -> None:
    wrapper, image, events = _fake_checkout(tmp_path, fail_at)
    completed = subprocess.run(
        [
            PWSH or "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(wrapper),
            "-Image",
            str(image),
            "-Prompt",
            "A tree gently sways.",
            "-ExpectedPinnedImage",
            PINNED,
            "-OutputDir",
            str(tmp_path / "results"),
            "-RunId",
            "offline-test",
            "-NonInteractive",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (completed.returncode == 0) is should_pass, (
        completed.stdout + completed.stderr
    )
    actual_events = (
        events.read_text(encoding="utf-8").splitlines() if events.exists() else []
    )
    assert actual_events == expected_events

def test_salad_manager_status_is_captureable_by_controlled_smoke() -> None:
    """The real manager must emit Status on the success pipeline, not Write-Host."""
    manager = (ROOT / "scripts" / "salad" / "manage_salad_worker.ps1").read_text(
        encoding="utf-8"
    )
    status_function = manager.split("function Show-Status {", maxsplit=1)[1].split(
        "\nAssert-ServiceDefinition", maxsplit=1
    )[0]
    assert 'Write-Output "service=$Service group=$GroupName status=missing"' in status_function
    assert 'Write-Output ("service={0} group={1} status={2}' in status_function
    assert "Write-Host" not in status_function

def test_static_i2v_comparison_without_baseline_never_allocates_gpu(tmp_path: Path) -> None:
    wrapper, image, events = _fake_checkout(tmp_path, None)
    completed = subprocess.run(
        [
            PWSH or "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(wrapper),
            "-Image",
            str(image),
            "-Prompt",
            "The camera remains fixed.",
            "-ExpectedPinnedImage",
            PINNED,
            "-OutputDir",
            str(tmp_path / "outputs"),
            "-I2VProfile",
            "static-camera",
            "-CompareWithRunId",
            "i2v-smoke-003",
            "-RunId",
            "static-trial-001",
            "-NonInteractive",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "Comparison baseline must contain a technically validated MP4" in (
        completed.stdout + completed.stderr
    )
    assert not events.exists()



def test_fresh_default_quality_gate_rejects_missing_baseline_before_gpu(
    tmp_path: Path,
) -> None:
    wrapper, image, events = _fake_checkout(tmp_path, None)
    result = subprocess.run(
        [
            PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(wrapper), "-Image", str(image), "-Prompt", "A steady scene.",
            "-ExpectedPinnedImage", PINNED,
            "-I2VProfile", "default-silent-verify",
            "-RequireExactBaselineVideoSha", "-NonInteractive",
        ],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "-RequireExactBaselineVideoSha requires" in result.stdout + result.stderr
    assert not events.exists()


@pytest.mark.parametrize("matches_baseline", [True, False])
def test_fresh_default_quality_gate_checks_real_file_sha_and_always_stops_gpu(
    tmp_path: Path, matches_baseline: bool
) -> None:
    """All capacity, R2, GPU and media calls are local fakes; no external requests."""
    wrapper, image, events = _fake_checkout(tmp_path, None)
    repo = wrapper.parents[2]
    inputs = tmp_path / "smoke-results" / "original-baseline" / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "keyframe.png").write_bytes(image.read_bytes())
    prompt = "A steady scene."
    (inputs / "video_prompts.json").write_text(
        json.dumps([{"shot_id": 1, "prompt": prompt}]), encoding="utf-8"
    )
    (inputs / "shot_timings.json").write_text(
        json.dumps([{"shot_id": 1, "start_seconds": 0, "end_seconds": 2}]),
        encoding="utf-8",
    )
    original_video = inputs.parent / "output" / "video_clips" / "shot_001.mp4"
    original_video.parent.mkdir(parents=True)
    original_video.write_bytes(b"original-video-output")
    original_sha = hashlib.sha256(original_video.read_bytes()).hexdigest()
    (inputs.parent / "i2v-smoke-report.json").write_text(
        json.dumps({
            "status": "technically_valid_visual_review_pending",
            "video_sha256": original_sha,
        }),
        encoding="utf-8",
    )

    smoke = repo / "scripts" / "smoke" / "ltx25_i2v_smoke.py"
    smoke.write_text(
        "import hashlib, json, shutil, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "out = Path(args[args.index('--output-dir') + 1])\n"
        "if args[0] == 'prepare':\n"
        "    inputs = out / 'inputs'\n"
        "    inputs.mkdir(parents=True, exist_ok=True)\n"
        "    shutil.copyfile(args[args.index('--image') + 1], inputs / 'keyframe.png')\n"
        "    prompt = args[args.index('--prompt') + 1]\n"
        "    (inputs / 'video_prompts.json').write_text("
        "json.dumps([{'shot_id': 1, 'prompt': prompt}]))\n"
        "    (inputs / 'shot_timings.json').write_text("
        "json.dumps([{'shot_id': 1, 'start_seconds': 0, 'end_seconds': 2}]))\n"
        "else:\n"
        "    video = Path(args[args.index('--video') + 1])\n"
        "    sha = hashlib.sha256(video.read_bytes()).hexdigest()\n"
        "    (out / 'i2v-smoke-report.json').write_text(json.dumps({"
        "'status': 'technically_valid_visual_review_pending',"
        "'video_sha256': sha}))\n",
        encoding="utf-8",
    )
    pipeline = repo / "scripts" / "pipeline" / "run_phase8_videos.py"
    content = "original-video-output" if matches_baseline else "new-different-video"
    pipeline.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "out = Path(args[args.index('--output-dir') + 1])\n"
        "(out / 'video_clips').mkdir(parents=True, exist_ok=True)\n"
        f"(out / 'video_clips' / 'shot_001.mp4').write_bytes({content!r}.encode())\n",
        encoding="utf-8",
    )
    result_dir = tmp_path / "smoke-results" / "fresh-verification"
    result = subprocess.run(
        [
            PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(wrapper), "-Image", str(image), "-Prompt", prompt,
            "-ExpectedPinnedImage", PINNED,
            "-I2VProfile", "default-silent-verify",
            "-RequireExactBaselineVideoSha",
            "-CompareWithRunId", "original-baseline",
            "-OutputDir", str(tmp_path / "smoke-results"),
            "-RunId", "fresh-verification", "-NonInteractive",
        ],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert (result.returncode == 0) is matches_baseline, (
        result.stdout + result.stderr
    )
    actual_events = events.read_text(encoding="utf-8").splitlines()
    assert actual_events == ["ltx25_i2v Start", "ltx25_i2v Stop"]
    receipt = json.loads(
        (result_dir / "quality-equivalence.json").read_text(encoding="utf-8")
    )
    assert receipt["reference_video_sha256"] == original_sha
    assert receipt["exact_mp4_sha256_match"] is matches_baseline
    if not matches_baseline:
        assert "VISUAL QUALITY GATE FAILED" in result.stdout + result.stderr
