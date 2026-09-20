from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.compositor.media import MediaProbe
from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoClip, VideoPrompt
import ai_video_factory.workflows.video_generation as generation
import ai_video_factory.workflows.video_upscale as upscale


def _write_png(path: Path, *, width: int, height: int, value: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = BytesIO()
    Image.new("RGB", (width, height), color=(value, value, value)).save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())


def _generation_inputs(base_dir: Path) -> tuple[list[StoryboardKeyframe], list[VideoPrompt], list[ShotTiming]]:
    return (
        [StoryboardKeyframe(shot_id=1, uri="storyboard_keyframes/shot_001.png")],
        [VideoPrompt(shot_id=1, prompt="Slow cinematic push-in with gentle parallax.")],
        [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=1.0)],
    )


def test_ltx_plan_accepts_native_landscape_keyframe_and_pins_720p24(tmp_path: Path) -> None:
    base_dir = tmp_path / "phase6"
    _write_png(
        base_dir / "storyboard_keyframes" / "shot_001.png",
        width=1536,
        height=864,
    )
    keyframes, prompts, timings = _generation_inputs(base_dir)

    plan = generation.build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base_dir,
    )

    assert plan[0].request.parameters["width"] == 1280
    assert plan[0].request.parameters["height"] == 720
    assert plan[0].request.parameters["fps"] == 24


def test_ltx_plan_rejects_portrait_or_undersized_keyframes(tmp_path: Path) -> None:
    base_dir = tmp_path / "phase6"
    keyframe_path = base_dir / "storyboard_keyframes" / "shot_001.png"
    keyframes, prompts, timings = _generation_inputs(base_dir)

    _write_png(keyframe_path, width=864, height=1536)
    with pytest.raises(ValueError, match="native 16:9"):
        generation.build_video_generation_plan(
            keyframes,
            prompts,
            timings,
            keyframe_base_dir=base_dir,
        )

    _write_png(keyframe_path, width=640, height=360)
    with pytest.raises(ValueError, match="below the 1280x720"):
        generation.build_video_generation_plan(
            keyframes,
            prompts,
            timings,
            keyframe_base_dir=base_dir,
        )


def test_ltx_manifest_archives_inactive_previous_plan_but_blocks_active_one(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "phase6"
    keyframe_path = base_dir / "storyboard_keyframes" / "shot_001.png"
    manifest_path = tmp_path / "phase8" / "video_generation_manifest.json"
    keyframes, prompts, timings = _generation_inputs(base_dir)

    _write_png(keyframe_path, width=1536, height=864, value=10)
    first_plan = generation.build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base_dir,
    )
    first_manifest = generation._load_or_create_manifest(first_plan, manifest_path)
    first_manifest.jobs[0].transport_status = "succeeded"
    generation._write_manifest(manifest_path, first_manifest)

    _write_png(keyframe_path, width=1536, height=864, value=20)
    second_plan = generation.build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base_dir,
    )
    second_manifest = generation._load_or_create_manifest(second_plan, manifest_path)

    assert second_manifest.run_fingerprint != first_manifest.run_fingerprint
    assert len(list(manifest_path.parent.glob("video_generation_manifest.archive-*.json"))) == 1

    second_manifest.jobs[0].transport_status = "running"
    second_manifest.jobs[0].transport_job_id = "transport-active"
    generation._write_manifest(manifest_path, second_manifest)
    _write_png(keyframe_path, width=1536, height=864, value=30)
    third_plan = generation.build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base_dir,
    )
    with pytest.raises(ValueError, match="active transports"):
        generation._load_or_create_manifest(third_plan, manifest_path)


def _probe_720p(_: Path) -> MediaProbe:
    return MediaProbe(
        codec_name="h264",
        width=1280,
        height=720,
        fps=24.0,
        duration_seconds=1.0,
        frame_count=24,
        audio_stream_count=0,
    )


def test_upscale_manifest_archives_inactive_previous_plan(tmp_path: Path, monkeypatch) -> None:
    phase8 = tmp_path / "phase8"
    source = phase8 / "video_clips" / "shot_001.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"first")
    monkeypatch.setattr(upscale, "probe_video", _probe_720p)

    clips = [VideoClip(shot_id=1, uri="video_clips/shot_001.mp4")]
    first_plan = upscale.build_video_upscale_plan(clips, clip_base_dir=phase8)
    manifest_path = phase8 / "video_upscale_manifest.json"
    first_manifest = upscale._load_or_create_manifest(first_plan, manifest_path)
    first_manifest.jobs[0].transport_status = "succeeded"
    upscale._write_manifest(manifest_path, first_manifest)

    source.write_bytes(b"second")
    second_plan = upscale.build_video_upscale_plan(clips, clip_base_dir=phase8)
    second_manifest = upscale._load_or_create_manifest(second_plan, manifest_path)

    assert second_manifest.run_fingerprint != first_manifest.run_fingerprint
    assert len(list(phase8.glob("video_upscale_manifest.archive-*.json"))) == 1
