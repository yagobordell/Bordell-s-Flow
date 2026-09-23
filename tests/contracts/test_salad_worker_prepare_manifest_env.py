from pathlib import Path

PREPARE_WRAPPER = Path("scripts/salad/prepare_salad_worker_manifest.ps1")


def test_prepare_wrapper_forces_manifest_runtime_environment() -> None:
    script = PREPARE_WRAPPER.read_text(encoding="utf-8")

    assert "$Definition.environment.PSObject.Properties" in script
    assert "[string]$Property.Value" in script
    assert 'Action = "Prepare"' in script
    assert "& $WorkerManager @Arguments" in script


def test_prepare_wrapper_restores_local_environment() -> None:
    script = PREPARE_WRAPPER.read_text(encoding="utf-8")

    assert "$Previous[$Name]" in script
    assert "finally {" in script
    assert "$Previous.GetEnumerator()" in script
    assert "$Entry.Value" in script


def test_prepare_wrapper_supports_skip_build_without_repair() -> None:
    script = PREPARE_WRAPPER.read_text(encoding="utf-8")

    assert '$Arguments["SkipBuild"] = $true' in script
    assert "manage_salad_stack.ps1" not in script
    assert "Repair" not in script
