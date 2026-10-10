"""Check E2 samples the actual 24-fps LTX clip, never the 30-fps preview."""
from __future__ import annotations

from types import SimpleNamespace

from ai_video_factory.editorial.contracts import E1Output
from ai_video_factory.editorial.frames import extract_frames


def test_e2_frame_samples_stay_within_final_24fps_render(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"unchanged-original-LTX")
    timeline = {
        "fps": 30,
        "duration_seconds": 2.0,
        "beats": [{
            "beat_id": "1A",
            "start_seconds": 0.0,
            "end_seconds": 2.0,
            "start_frame": 0,
            "end_frame": 60,
            "render_start_frame": 0,
            "render_end_frame": 48,
        }],
    }
    intervention = E1Output.model_validate({
        "schema_version": "e1-v1",
        "interventions": [{
            "id": "highlight-cloned",
            "kind": "progressive_text",
            "elements": [
                {"text": "CLONED", "anchor": {"beat_id": "1A", "phrase": "cloned"}},
                {"text": "VOICE", "anchor": {"beat_id": "1A", "phrase": "voice"}},
            ],
            "end_beat_id": "1A",
            "layout_intent": {"placement": "center", "scale": "large",
                              "color": "#FFFFFF"},
            "animation": "fade",
        }],
    }).interventions[0]
    word_index = {
        "1A": [
            {"token": "cloned", "start_seconds": 0.5, "end_seconds": 0.8},
            {"token": "voice", "start_seconds": 0.9, "end_seconds": 1.2},
        ],
    }
    samples = []

    def fake_ffmpeg(args, **_kwargs):
        destination = args[-1]
        samples.append(float(args[args.index("-ss") + 1]))
        from pathlib import Path

        Path(destination).write_bytes(b"verified-jpeg-test")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(
        "ai_video_factory.editorial.frames.subprocess.run", fake_ffmpeg,
    )
    frames = extract_frames(video, tmp_path, intervention, word_index, timeline)
    numbers = [int(name.removeprefix("frame_")) for name, _ in frames]
    assert numbers[0] == 12
    assert numbers[-1] == 47
    assert all(12 <= number <= 47 for number in numbers)
    assert all(second < 2.0 for second in samples)
