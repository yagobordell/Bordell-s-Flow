from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_video_factory.compositor import (
    CompositionPlan,
    build_final_mux_command,
    mux_final_video,
    validate_final_mux_inputs,
)
from ai_video_factory.config import settings
from ai_video_factory.domain import FinalVideo, NarrationAudio


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mux the Phase 9.4 visual with canonical Phase 5 narration."
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=settings.output_dir / "phase9" / "composition_plan.json",
    )
    parser.add_argument(
        "--visual",
        type=Path,
        default=settings.output_dir / "phase9" / "visual_motion.mp4",
    )
    parser.add_argument(
        "--narration-metadata",
        type=Path,
        default=settings.output_dir / "phase5" / "narration.json",
    )
    parser.add_argument(
        "--audio",
        type=Path,
        default=settings.output_dir / "phase5" / "narration.wav",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase9" / "final_video.mp4",
    )
    parser.add_argument(
        "--metadata-output",
        type=Path,
        default=settings.output_dir / "phase9" / "final_video.json",
    )
    parser.add_argument("--audio-bitrate-kbps", type=int, default=192)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    plan = _read_plan(args.plan)
    narration = _read_narration(args.narration_metadata)

    inputs = validate_final_mux_inputs(plan, narration, args.visual, args.audio)
    print(
        "Phase 9.5 inputs: "
        f"frames={plan.total_frames} duration={inputs.canonical_duration_seconds:.3f}s "
        f"fps={plan.fps} narration_wav={inputs.narration.duration_seconds:.6f}s "
        f"narration_metadata={narration.duration_seconds:.6f}s"
    )
    print(
        "audio_input="
        f"{inputs.narration.sample_rate}Hz/{inputs.narration.channels}ch "
        f"pcm_width={inputs.narration.sample_width_bytes * 8}bit"
    )

    if args.prepare_only:
        command = build_final_mux_command(
            args.visual,
            args.audio,
            args.output,
            duration_seconds=inputs.canonical_duration_seconds,
            audio_bitrate_kbps=args.audio_bitrate_kbps,
        )
        print("video_codec=copy audio_codec=aac")
        print("ffmpeg=" + " ".join(command))
        print("Phase 9.5 final mux inputs: OK")
        return

    result = mux_final_video(
        plan,
        narration,
        args.visual,
        args.audio,
        args.output,
        audio_bitrate_kbps=args.audio_bitrate_kbps,
    )

    final_video = FinalVideo(
        uri=_artifact_uri(args.output, metadata_dir=args.metadata_output.parent),
        duration_seconds=result.canonical_duration_seconds,
    )
    args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_output.write_text(
        json.dumps(final_video.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    frame_count = result.video.frame_count
    if frame_count is None:
        frame_count = round(result.video.duration_seconds * plan.fps)

    print(f"Phase 9.5 final video: {args.output.resolve()}")
    print(f"Phase 9.5 metadata: {args.metadata_output.resolve()}")
    print(
        f"frames={frame_count} duration={result.video.duration_seconds:.3f}s "
        f"fps={result.video.fps:g} video_codec={result.video.codec_name} "
        f"audio_streams={result.video.audio_stream_count} "
        f"audio_codec={result.audio.codec_name}"
    )
    print(
        f"audio={result.audio.sample_rate}Hz/{result.audio.channels}ch "
        f"bitrate_target={args.audio_bitrate_kbps}k"
    )
    print("Phase 9.5 final narration mux: OK")


def _read_plan(path: Path) -> CompositionPlan:
    if not path.is_file():
        raise SystemExit(f"Required composition plan not found: {path}")
    return CompositionPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _read_narration(path: Path) -> NarrationAudio:
    if not path.is_file():
        raise SystemExit(f"Required narration metadata not found: {path}")
    return NarrationAudio.model_validate_json(path.read_text(encoding="utf-8"))


def _artifact_uri(path: Path, *, metadata_dir: Path) -> str:
    resolved_path = path.resolve()
    resolved_dir = metadata_dir.resolve()
    try:
        return resolved_path.relative_to(resolved_dir).as_posix()
    except ValueError:
        return resolved_path.as_posix()


if __name__ == "__main__":
    main()
