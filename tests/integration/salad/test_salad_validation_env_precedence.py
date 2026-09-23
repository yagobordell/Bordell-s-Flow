from pathlib import Path

VALIDATION_MANAGER = Path("scripts/salad/manage_salad_validation.ps1")


def test_prepare_uses_manifest_environment_over_local_env() -> None:
    script = VALIDATION_MANAGER.read_text(encoding="utf-8")

    assert "function Use-ManifestEnvironment" in script
    assert 'PSObject.Properties["shared_environment"]' in script
    assert "$Definition.environment.PSObject.Properties" in script
    assert "[string]$Property.Value" in script
    assert "if ($StackAction -eq \"Prepare\")" in script
    assert "Use-ManifestEnvironment -ScriptBlock $InvokeStack" in script


def test_manifest_environment_is_restored_after_prepare() -> None:
    script = VALIDATION_MANAGER.read_text(encoding="utf-8")

    assert "finally {" in script
    assert "$Previous.GetEnumerator()" in script
    assert "$Entry.Value" in script
