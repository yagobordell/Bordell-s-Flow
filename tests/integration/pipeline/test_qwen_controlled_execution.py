from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(
    PWSH is None or os.name == "nt",
    reason="This isolated PowerShell test uses a POSIX fake-python executable",
)


def _quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.parametrize("phase", ["Phase 4", "Phase 6"])
@pytest.mark.parametrize("status", ["hit", "invalid"])
def test_qwen_cache_gate_never_allocates_gpu(
    tmp_path: Path, phase: str, status: str
) -> None:
    pipeline = tmp_path / "scripts" / "pipeline"
    pipeline.mkdir(parents=True)
    shared = pipeline / "_qwen_controlled.ps1"
    shared.write_bytes((ROOT / "scripts/pipeline/_qwen_controlled.ps1").read_bytes())

    manifest = tmp_path / "deploy" / "salad" / "services.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "stack": {"organization": "offline-test", "project": "offline-test"},
                "services": {"qwen_image_21": {"queue_name": "offline-test-queue"}},
            }
        ),
        encoding="utf-8",
    )

    salad = tmp_path / "scripts" / "salad"
    salad.mkdir()
    gpu_marker = tmp_path / "gpu-touched"
    (salad / "start_salad_optimized_prewarm.ps1").write_text(
        'Set-Content -LiteralPath $env:AVF_GPU_MARKER -Value "unsafe"\n'
        'throw "Prewarm must not run in offline cache test"\n',
        encoding="utf-8",
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'printf "%s\n" "$(basename "$1")" >> "$AVF_FAKE_CALLS"\n'
        'case "$1" in\n'
        '  *audit_cache.py)\n'
        '    while [ "$#" -gt 0 ]; do\n'
        '      if [ "$1" = "--json-output" ]; then\n'
        '        printf \'[{"status":"%s"}]\\n\' "$AVF_FAKE_STATUS" > "$2"\n'
        "        break\n"
        "      fi\n"
        "      shift\n"
        "    done\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    reference = tmp_path / "reference.json"
    reference.write_text("{}", encoding="utf-8")
    shots = tmp_path / "shots.json"
    shots.write_text("{}", encoding="utf-8")
    required = f"@({_quote(reference)}, {_quote(shots)})" if phase == "Phase 6" else (
        f"@({_quote(reference)})"
    )
    calls = tmp_path / "python-calls"
    runner_name = "run_phase6_keyframes.py" if phase == "Phase 6" else "run_phase4_assets.py"
    command = (
        "$ErrorActionPreference = 'Stop'\n"
        f"if ((Get-Command python).Source -ne {_quote(fake_python)}) "
        "{ throw 'Fake Python is not selected' }\n"
        "$Arguments = @{\n"
        f"  Phase = {_quote(phase)}\n"
        f"  RequiredInputs = {required}\n"
        f"  CacheAuditScript = {_quote(tmp_path / 'audit_cache.py')}\n"
        f"  CacheAuditArguments = @({_quote(reference)})\n"
        f"  RunnerScript = {_quote(tmp_path / runner_name)}\n"
        f"  RunnerArguments = @({_quote(reference)})\n"
        "  PrewarmTimeoutMinutes = 10\n"
        "  NonInteractive = $true\n"
        "}\n"
        f"& {_quote(shared)} @Arguments\n"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "AVF_FAKE_CALLS": str(calls),
            "AVF_FAKE_STATUS": status,
            "AVF_GPU_MARKER": str(gpu_marker),
        }
    )
    result = subprocess.run(
        [PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    expected_calls = ["check_r2_ready.py", "audit_cache.py"]
    if status == "hit":
        expected_calls.append(runner_name)
        assert result.returncode == 0, result.stderr or result.stdout
    else:
        assert result.returncode != 0
        assert "cache contains invalid entries" in result.stderr + result.stdout
    assert calls.read_text(encoding="utf-8").splitlines() == expected_calls
    assert not gpu_marker.exists()
