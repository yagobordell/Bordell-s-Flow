from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    ObjectInput,
    ObjectOutput,
)
from ai_video_factory.workflows.r2_run_scope import (
    reset_active_video_scope,
    saved_video_scope,
    scope_inference_request,
    set_active_video_scope,
    video_run_scope,
)


def test_run_scope_persists_across_resumes_and_isolated_outputs(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    first = video_run_scope(left, left / "video" / "runs" / "abc")
    assert first is not None
    assert first == saved_video_scope(left)
    assert first == video_run_scope(left, left / "video" / "runs" / "abc")
    assert first != video_run_scope(right, right / "video" / "runs" / "abc")


def test_legacy_manifest_does_not_receive_new_scope(tmp_path: Path) -> None:
    run = tmp_path / "video" / "runs" / "legacy"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text("{}", encoding="utf-8")
    assert video_run_scope(tmp_path, run) is None
    assert saved_video_scope(tmp_path) is None


def test_gpu_request_scoping_preserves_voice_reference_and_schema() -> None:
    base_id = "fish-test-identity"
    request = InferenceJobRequest(
        job_id=base_id,
        task="fish.speech",
        inputs=[ObjectInput(
            name="reference_audio",
            key="voices/project-narrator-v1.wav",
            sha256="a" * 64,
        )],
        output=ObjectOutput(
            key=f"jobs/{base_id}/narration.wav",
            content_type="audio/wav",
        ),
    )
    token = set_active_video_scope("b" * 24)
    try:
        scoped = scope_inference_request(request)
        assert scoped.job_id == base_id + "-r" + "b" * 24
        assert scoped.inputs[0].key == "voices/project-narrator-v1.wav"
        assert scoped.output.key == f"jobs/{scoped.job_id}/narration.wav"
        assert scoped.sidecar_outputs is None
        assert InferenceJobRequest.model_validate(scoped.model_dump()) == scoped
        assert scope_inference_request(scoped) == scoped
    finally:
        reset_active_video_scope(token)
    assert scope_inference_request(request) == request


def test_scoped_inputs_outputs_and_sidecars_are_private() -> None:
    request = InferenceJobRequest(
        job_id="ltx-test",
        task="ltx.a2v",
        inputs=[
            ObjectInput(name="image", key="cta/inputs/images/old.png"),
            ObjectInput(name="audio", key="cta/inputs/audio/old.wav"),
        ],
        output=ObjectOutput(
            key="jobs/ltx-test/avatar.mp4", content_type="video/mp4",
        ),
        sidecar_outputs={"metadata": ObjectOutput(
            key="jobs/ltx-test/metadata.json", content_type="application/json",
        )},
    )
    token = set_active_video_scope("c" * 24)
    try:
        scoped = scope_inference_request(request)
    finally:
        reset_active_video_scope(token)
    prefix = f"jobs/{scoped.job_id}/"
    assert [item.key for item in scoped.inputs] == [
        prefix + "inputs/image.png", prefix + "inputs/audio.wav",
    ]
    assert scoped.output.key == prefix + "avatar.mp4"
    assert scoped.sidecar_outputs["metadata"].key == prefix + "metadata.json"
    assert InferenceJobRequest.model_validate(scoped.model_dump()) == scoped


def test_invalid_scope_marker_fails_closed(tmp_path: Path) -> None:
    marker = tmp_path / "video" / "r2_run_scope.txt"
    marker.parent.mkdir()
    marker.write_text("../../unsafe", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid"):
        saved_video_scope(tmp_path)
