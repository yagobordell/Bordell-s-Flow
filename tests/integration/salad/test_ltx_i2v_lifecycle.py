"""Active Salad manager status should be readable by service checks."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_salad_worker_status_is_machine_readable() -> None:
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

