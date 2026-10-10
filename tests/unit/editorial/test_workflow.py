"""Offline end-to-end editorial orchestration with fake media and no paid inference."""
from __future__ import annotations

import hashlib
import json

from ai_video_factory.editorial.contracts import E1Output, E2Output
from ai_video_factory.editorial.workflow import generate_editorial_video


def test_editorial_orchestration_does_not_modify_original_media(tmp_path, monkeypatch):
    from ai_video_factory.editorial import workflow

    (tmp_path / "visual_plan.json").write_text(json.dumps({
        "visual_style": "profesional",
        "narrative_core": {"central_question": "Q", "final_answer": "A"},
        "blocks": [{"block_id": 1, "type": "intro", "beats": [
            {"beat_id": "1A", "text": "cloned voice", "visual_type": "media_image",
             "prompt": "A person holding a phone"},
        ]}],
    }), encoding="utf-8")
    run_dir = tmp_path / "video" / "runs" / "fingerprint"
    run_dir.mkdir(parents=True)
    video = run_dir / "final.mp4"
    video.write_bytes(b"verified-existing-LTX-video")
    old_sha = hashlib.sha256(video.read_bytes()).hexdigest()
    timeline = {
        "fps": 24, "duration_seconds": 2.0,
        "beats": [{"beat_id": "1A", "start_frame": 0, "end_frame": 48}],
    }
    (run_dir / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    report = {"run": {
        "script_sha256": "original",
        "video_generation": {
            "status": "completed", "file": "video/runs/fingerprint/final.mp4",
            "sha256": old_sha, "timeline_file": "video/runs/fingerprint/timeline.json",
        },
        "audio_generation": {"status": "completed", "sha256": "saved-original-PCM"},
    }}
    called = {"e1": 0, "e2": 0, "overlay": 0, "composite": 0}

    async def fake_e1(*args, **kwargs):
        called["e1"] += 1
        return E1Output.model_validate({
            "schema_version": "e1-v1", "interventions": [{
                "id": "edit-001", "kind": "progressive_text",
                "elements": [
                    {"text": "CLONED", "anchor": {"beat_id": "1A", "phrase": "cloned"}},
                    {"text": "VOICE", "anchor": {"beat_id": "1A", "phrase": "voice"}},
                ],
                "end_beat_id": "1A",
                "layout_intent": {"placement": "center", "scale": "large",
                                  "color": "#FFFFFF"},
                "animation": "fade",
            }],
        }), "e1-test"

    async def fake_e2(*args, **kwargs):
        called["e2"] += 1
        return E2Output.model_validate({
            "schema_version": "e2-v1", "decisions": [{
                "id": "edit-001", "status": "approved",
                "reason": "Center available",
                "x": 0.1, "y": 0.3, "width": 0.8, "height": 0.3,
                "font_size_px": 84, "color": "#FFFFFF",
                "stroke_color": "#000000", "stroke_width_px": 2,
                "align": "center",
            }],
        }), "e2-test"

    def render(_input, folder):
        called["overlay"] += 1
        folder.mkdir(parents=True)
        (folder / "layers.json").write_text('{"clips": []}', encoding="utf-8")

    def composite(_base, _layers, destination, _events, _frames, _seconds):
        called["composite"] += 1
        destination.write_bytes(b"edited-video-with-audio")

    monkeypatch.setattr(workflow, "preflight_editorial_renderer", lambda: None)
    monkeypatch.setattr(workflow, "_probe_final", lambda *args: 2.0)
    monkeypatch.setattr(workflow, "build_word_index", lambda *args: {"1A": [
        {"token": "cloned", "start_seconds": 0.5, "end_seconds": 0.8},
        {"token": "voice", "start_seconds": 0.9, "end_seconds": 1.2},
    ]})
    monkeypatch.setattr(workflow, "run_e1", fake_e1)
    monkeypatch.setattr(workflow, "run_e2", fake_e2)
    monkeypatch.setattr(workflow, "extract_frames", lambda *args: [])
    monkeypatch.setattr(workflow, "_fits", lambda *args: True)
    monkeypatch.setattr(workflow, "_render_overlay", render)
    monkeypatch.setattr(workflow, "_composite", composite)

    result = generate_editorial_video(tmp_path, report)
    assert result["status"] == "completed"
    assert result["approved_effects"] == 1
    assert called == {"e1": 1, "e2": 1, "overlay": 1, "composite": 1}
    assert hashlib.sha256(video.read_bytes()).hexdigest() == old_sha
    assert result["source_video_sha256"] == old_sha
    assert result["source_audio_sha256"] == "saved-original-PCM"
    assert (tmp_path / result["file"]).read_bytes() == b"edited-video-with-audio"
    again = generate_editorial_video(tmp_path, report)
    assert again == result
    assert called["overlay"] == 1
    assert called["composite"] == 1



def test_remotion_uses_absolute_paths_when_node_changes_working_directory(
    tmp_path, monkeypatch,
):
    """Regression: edit-only work paths must not resolve under remotion/editorial."""
    from pathlib import Path
    from types import SimpleNamespace

    from ai_video_factory.editorial import workflow

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        workflow, "__file__",
        str(tmp_path / "src" / "ai_video_factory" / "editorial" / "workflow.py"),
    )
    package = tmp_path / "remotion" / "editorial"
    (package / "node_modules" / "remotion").mkdir(parents=True)

    relative_run = Path(".video_factory_work/enzo/video02/editorial/runs/test")
    relative_run.mkdir(parents=True)
    plan = relative_run / "render_plan.json"
    layers = relative_run / "layers"
    plan.write_text('{"schema_version": "editorial-render-v1"}', encoding="utf-8")

    def fake_node(argv, *, cwd, capture_output, text, check):
        assert cwd == package
        assert argv[0] == "node"
        node_plan, node_layers = Path(argv[2]), Path(argv[3])
        assert node_plan.is_absolute()
        assert node_layers.is_absolute()
        assert node_plan.read_text(encoding="utf-8") == plan.read_text(encoding="utf-8")
        assert node_layers == layers.resolve()
        node_layers.mkdir(parents=True)
        (node_layers / "layers.json").write_text('{"clips":[]}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(workflow.subprocess, "run", fake_node)
    workflow._render_overlay(plan, layers)
    assert (layers / "layers.json").is_file()
