from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SALAD_SCRIPTS = ROOT / "scripts" / "salad"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="PowerShell 7 is not installed")


def _quote(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(script: str) -> None:
    completed = subprocess.run(
        [PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_shared_environment_loader_preserves_process_values_and_parses_quotes(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "AVF_SHARED_EXISTING=from-file\n"
        'AVF_SHARED_NEW="quoted value"\n'
        "export AVF_SHARED_EXPORTED='from export'\n"
        "not an environment entry\n",
        encoding="utf-8",
    )
    _run_powershell(
        f"$ErrorActionPreference = 'Stop'\n"
        f"$RepoRoot = {_quote(tmp_path)}\n"
        f". {_quote(SALAD_SCRIPTS / '_env_file.ps1')}\n"
        "$env:AVF_SHARED_EXISTING = 'from-process'\n"
        "$env:AVF_SHARED_NEW = ''\n"
        "$env:AVF_SHARED_EXPORTED = ''\n"
        "Import-EnvFile -Path '.env'\n"
        "Import-EnvFile -Path '.missing'\n"
        "if ($env:AVF_SHARED_EXISTING -ne 'from-process') { throw 'env precedence' }\n"
        "if ($env:AVF_SHARED_NEW -ne 'quoted value') { throw 'double quotes' }\n"
        "if ($env:AVF_SHARED_EXPORTED -ne 'from export') { throw 'export quotes' }\n"
    )


def test_shared_http_status_supports_missing_and_numeric_status() -> None:
    _run_powershell(
        f"$ErrorActionPreference = 'Stop'\n"
        f". {_quote(SALAD_SCRIPTS / '_http_status.ps1')}\n"
        "Set-StrictMode -Version Latest\n"
        "$known = [pscustomobject]@{Exception = [pscustomobject]@{"
        "Response = [pscustomobject]@{StatusCode = 503}}}\n"
        "if ((Get-HttpStatusCode -ErrorRecord $known) -ne 503) "
        "{ throw 'status extraction' }\n"
        "$unknown = [pscustomobject]@{Exception = [pscustomobject]@{Response = $null}}\n"
        "if ($null -ne (Get-HttpStatusCode -ErrorRecord $unknown)) "
        "{ throw 'missing status' }\n"
        "$absent = [pscustomobject]@{Exception = [pscustomobject]@{Message = 'network unavailable'}}\n"
        "if ($null -ne (Get-HttpStatusCode -ErrorRecord $absent)) "
        "{ throw 'missing HTTP Response property' }\n"
        "$missing_exception = [pscustomobject]@{Message = 'unexpected local error'}\n"
        "if ($null -ne (Get-HttpStatusCode -ErrorRecord $missing_exception)) "
        "{ throw 'missing Exception property' }\n"
        "$missing_code = [pscustomobject]@{Exception = [pscustomobject]@{"
        "Response = [pscustomobject]@{Message = 'proxy error'}}}\n"
        "if ($null -ne (Get-HttpStatusCode -ErrorRecord $missing_code)) "
        "{ throw 'missing StatusCode property' }\n"
        "Set-StrictMode -Version Latest\n"
        "try { throw 'original-transport-failure' } catch { "
        "if ($null -ne (Get-HttpStatusCode -ErrorRecord $_)) "
        "{ throw 'non-HTTP exception must return null' }; "
        "if ($_.Exception.Message -ne 'original-transport-failure') "
        "{ throw 'original error was obscured' } }\n"
    )


def test_try_get_group_preserves_original_transport_exception() -> None:
    manager = (SALAD_SCRIPTS / "manage_salad_worker.ps1").read_text(encoding="utf-8")
    group_reader = "function Try-Get-Group {" + manager.split(
        "function Try-Get-Group {", maxsplit=1
    )[1].split("function Get-Group {", maxsplit=1)[0]
    _run_powershell(
        "$ErrorActionPreference = 'Stop'\n"
        "Set-StrictMode -Version Latest\n"
        f". {_quote(SALAD_SCRIPTS / '_http_status.ps1')}\n"
        "$ContainersBase = 'https://example.invalid'\n"
        "$GroupName = 'ltx25-i2v-test'\n"
        "function Invoke-SaladRequest { "
        "param([hashtable]$Headers, [string]$Uri, [string]$Operation) "
        "throw 'actual-salad-transport-failure' }\n"
        + group_reader
        + "\n"
        + "try { Try-Get-Group -Headers @{}; throw 'must have failed' } "
        + "catch { if ($_.Exception.Message -ne 'actual-salad-transport-failure') "
        + "{ throw \"Original failure was masked: $($_.Exception.Message)\" } }\n"
        + "exit 0\n"
    )


def test_salad_manager_uses_shared_helpers() -> None:
    source = (SALAD_SCRIPTS / "manage_salad_worker.ps1").read_text(encoding="utf-8")

    assert '. (Join-Path $PSScriptRoot "_env_file.ps1")' in source
    assert '. (Join-Path $PSScriptRoot "_http_status.ps1")' in source
    assert "function Import-EnvFile {" not in source
    assert "function Get-HttpStatusCode {" not in source
