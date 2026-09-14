from pathlib import Path

SMOKE_SUITE = Path("scripts/run_salad_smoke_suite.py")
PHASE8_SMOKE = Path("scripts/submit_phase8_smoke.py")


def test_ltx_smoke_streams_child_output_instead_of_capturing_until_exit() -> None:
    text = SMOKE_SUITE.read_text(encoding="utf-8")
    ltx = text.split("def _smoke_ltx25", maxsplit=1)[1].split(
        "async def main", maxsplit=1
    )[0]

    assert "subprocess.Popen(" in ltx
    assert "stdout=subprocess.PIPE" in ltx
    assert "stderr=subprocess.STDOUT" in ltx
    assert 'print(line, end="", flush=True)' in ltx
    assert "capture_output=True" not in ltx


def test_phase8_smoke_records_request_before_external_submission() -> None:
    text = PHASE8_SMOKE.read_text(encoding="utf-8")

    request_write = 'request_path.write_text(json.dumps(body, indent=2) + "\\n"'
    r2_upload = '_event("R2_UPLOAD_START"'
    queue_submit = '_event("QUEUE_SUBMIT_START"'

    assert text.index(request_write) < text.index(r2_upload)
    assert text.index(request_write) < text.index(queue_submit)


def test_phase8_smoke_exposes_safe_stage_markers_and_pending_guard() -> None:
    text = PHASE8_SMOKE.read_text(encoding="utf-8")

    for marker in (
        "SMOKE_START",
        "REQUEST_BUILD_START",
        "REQUEST_BUILD_DONE",
        "R2_UPLOAD_START",
        "R2_UPLOAD_DONE",
        "QUEUE_PREFLIGHT_START",
        "QUEUE_PREFLIGHT_DONE",
        "QUEUE_SUBMIT_START",
        "QUEUE_SUBMIT_DONE",
        "QUEUE_WAIT_START",
        "QUEUE_WAIT_STATUS",
        "ARTIFACT_DOWNLOAD_START",
        "ARTIFACT_DOWNLOAD_DONE",
        "SMOKE_DONE",
    ):
        assert marker in text

    assert '"--pending-timeout-seconds"' in text
    assert "verify queue attachment/routing" in text
    assert "SALAD_API_KEY" not in text.split("def _event", maxsplit=1)[0]
