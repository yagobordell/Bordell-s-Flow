from pathlib import Path

SMOKE_SUITE = Path("scripts/smoke/run_salad_smoke_suite.py")
PHASE8_SMOKE = Path("scripts/smoke/submit_phase8_smoke.py")


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
    assert '_event("SMOKE_START", shot_id=args.shot_id, queue=args.queue_name)' in text



def test_phase8_smoke_defaults_to_canonical_landscape_contract() -> None:
    text = PHASE8_SMOKE.read_text(encoding="utf-8")

    assert 'parser.add_argument("--width", type=int, default=1280)' in text
    assert 'parser.add_argument("--height", type=int, default=720)' in text
    assert 'parser.add_argument("--fps", type=int, default=24)' in text
    assert "MP4 dimensions do not match the requested contract" in text
    assert "MP4 fps" in text



def test_ltx_suite_invokes_landscape_720p_smoke() -> None:
    text = SMOKE_SUITE.read_text(encoding="utf-8")
    ltx = text.split("def _smoke_ltx25", maxsplit=1)[1].split(
        "async def main", maxsplit=1
    )[0]

    assert '"--width",\n        "1280"' in ltx
    assert '"--height",\n        "720"' in ltx
    assert '"--width",\n        "768"' not in ltx
    assert '"--height",\n        "1280"' not in ltx
    assert "_prepare_ltx_landscape_keyframe(" in ltx
    assert 'inputs_dir / "ltx-keyframe-16x9.png"' in ltx


def test_phase8_smoke_requires_ffprobe_for_verified_success() -> None:
    text = PHASE8_SMOKE.read_text(encoding="utf-8")

    assert "ffprobe is required for Phase 8 smoke validation" in text
    assert "refusing unverified success" in text
    assert "if probe is not None:" not in text
