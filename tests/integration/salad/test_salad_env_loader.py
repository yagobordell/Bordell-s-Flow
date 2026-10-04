"""PowerShell env loader must see the same local and external DSNs as Python."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_PWSH = shutil.which("pwsh")
_LOADER = Path(__file__).resolve().parents[3] / "scripts/salad/_env_file.ps1"
pytestmark = pytest.mark.skipif(_PWSH is None, reason="PowerShell 7 required")


def _literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


@pytest.mark.parametrize(
    ("dsn_location", "expected"),
    [
        ("worktree", "postgresql://local.example.invalid/jobs"),
        ("external", "postgresql://external.example.invalid/jobs"),
        ("process", "postgresql://process.example.invalid/jobs"),
    ],
)
def test_salad_env_loader_finds_dsns(
    tmp_path: Path, dsn_location: str, expected: str
) -> None:
    worktree = tmp_path / "worktree"
    external = tmp_path / "main"
    (worktree / "config").mkdir(parents=True)
    (external / "config").mkdir(parents=True)

    if dsn_location == "worktree":
        (worktree / ".env").write_text("POSTGRES_DSN=" + expected + "\n")
    if dsn_location == "external":
        (external / "config" / "app.env").write_text("POSTGRES_DSN=" + expected + "\n")
    (external / "config" / "salad.env").write_text("SALAD_PROJECT=test-project\n")

    # Do not emit an actual DSN to CI logs. The strings here are test-only.
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$env:POSTGRES_DSN = "
        + ("'postgresql://process.example.invalid/jobs'" if dsn_location == "process" else "$null")
        + "\n"
        + "$RepoRoot = " + _literal(worktree) + "\n"
        + ". " + _literal(_LOADER) + "\n"
        + "Import-EnvFile -Path " + _literal(external / "config" / "salad.env") + "\n"
        + "if ($env:POSTGRES_DSN -ne '" + expected + "') { throw 'Wrong DSN source' }\n"
        + "Write-Output 'ENV_LOADER_OK'\n"
    )
    result = subprocess.run(
        [_PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "ENV_LOADER_OK" in result.stdout
