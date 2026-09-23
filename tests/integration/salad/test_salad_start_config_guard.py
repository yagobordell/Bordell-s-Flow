from pathlib import Path

START_SCRIPT = Path("scripts/salad/start_salad_scale_to_zero.ps1")


def test_scale_to_zero_start_requires_live_queue_config_to_match_manifest() -> None:
    script = START_SCRIPT.read_text(encoding="utf-8")

    assert "function Test-GroupQueueConfiguration" in script
    assert "function Assert-GroupQueueConfiguration" in script
    assert '$Group.PSObject.Properties["queue_connection"]' in script
    assert "$Connection.Value.queue_name" in script
    assert "$Connection.Value.path" in script
    assert "$Connection.Value.port" in script
    assert '$Group.PSObject.Properties["queue_autoscaler"]' in script
    assert "$Autoscaler.Value.min_replicas" in script
    assert "$Autoscaler.Value.max_replicas" in script
    assert "$Autoscaler.Value.desired_queue_length" in script
    assert "$Autoscaler.Value.polling_period" in script
    assert "$Autoscaler.Value.max_upscale_per_minute" in script
    assert "$Autoscaler.Value.max_downscale_per_minute" in script
    assert "$Group.queue_autoscaler" not in script
    assert (\n        "scripts/salad/repair_salad_queue_attachment.ps1 -Service $Service before Start"\n        in script\n    )


def test_scale_to_zero_start_checks_queue_config_before_starting_group() -> None:
    script = START_SCRIPT.read_text(encoding="utf-8")

    first_guard = script.index("Assert-GroupQueueConfiguration -Group $Group")
    start_call = script.index('-Uri "$GroupUrl/start"')
    polling_guard = script.rindex("Assert-GroupQueueConfiguration -Group $Group")

    assert first_guard < start_call < polling_guard
    assert '"User-Agent" = "ai-video-factory-scale-to-zero-starter/1.2"' in script
