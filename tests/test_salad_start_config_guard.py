from pathlib import Path

START_SCRIPT = Path("scripts/start_salad_scale_to_zero.ps1")


def test_scale_to_zero_start_requires_live_queue_config_to_match_manifest() -> None:
    script = START_SCRIPT.read_text(encoding="utf-8")

    assert "function Test-GroupQueueConfiguration" in script
    assert "function Assert-GroupQueueConfiguration" in script
    assert "queue_connection.queue_name" in script
    assert "queue_connection.path" in script
    assert "queue_connection.port" in script
    assert "queue_autoscaler.min_replicas" in script
    assert "queue_autoscaler.max_replicas" in script
    assert "queue_autoscaler.desired_queue_length" in script
    assert "queue_autoscaler.polling_period" in script
    assert "queue_autoscaler.max_upscale_per_minute" in script
    assert "queue_autoscaler.max_downscale_per_minute" in script
    assert "scripts/repair_salad_queue_attachment.ps1 -Service $Service before Start" in script


def test_scale_to_zero_start_checks_queue_config_before_starting_group() -> None:
    script = START_SCRIPT.read_text(encoding="utf-8")

    first_guard = script.index("Assert-GroupQueueConfiguration -Group $Group")
    start_call = script.index('-Uri "$GroupUrl/start"')
    polling_guard = script.rindex("Assert-GroupQueueConfiguration -Group $Group")

    assert first_guard < start_call < polling_guard
    assert '"User-Agent" = "ai-video-factory-scale-to-zero-starter/1.2"' in script
