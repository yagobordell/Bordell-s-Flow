import math

from ai_video_factory.bots.video_prompts import VideoPromptBot
from ai_video_factory.domain import Shot, ShotTiming, StoryboardFrame, VideoPrompt


async def build_video_prompts(
    shots: list[Shot],
    timings: list[ShotTiming],
    storyboard_frames: list[StoryboardFrame],
    *,
    prompt_bot: VideoPromptBot,
    visual_style: str,
    aspect_ratio: str,
) -> list[VideoPrompt]:
    """Build ordered motion prompts while carrying explicit within-scene temporal context."""

    _validate_inputs(shots, timings, storyboard_frames)

    prompts: list[VideoPrompt] = []
    previous_prompt: VideoPrompt | None = None
    previous_scene_id: int | None = None

    for shot, timing, storyboard_frame in zip(
        shots,
        timings,
        storyboard_frames,
        strict=True,
    ):
        if previous_scene_id is not None and shot.scene_id != previous_scene_id:
            previous_prompt = None

        prompt = await prompt_bot.run(
            shot,
            timing,
            storyboard_frame,
            visual_style=visual_style,
            aspect_ratio=aspect_ratio,
            previous_prompt=previous_prompt,
        )
        if not prompt.strip():
            raise ValueError("Video prompt bot returned an empty prompt")

        video_prompt = VideoPrompt(shot_id=shot.id, prompt=prompt.strip())
        prompts.append(video_prompt)
        previous_prompt = video_prompt
        previous_scene_id = shot.scene_id

    return prompts


def _validate_inputs(
    shots: list[Shot],
    timings: list[ShotTiming],
    storyboard_frames: list[StoryboardFrame],
) -> None:
    if not shots:
        raise ValueError("Video motion planning requires at least one shot")

    shot_ids = [shot.id for shot in shots]
    if shot_ids != list(range(1, len(shots) + 1)):
        raise ValueError("Video shots must have consecutive IDs starting at 1")

    scene_ids = [shot.scene_id for shot in shots]
    if scene_ids != sorted(scene_ids):
        raise ValueError("Video shots must preserve non-decreasing scene order")

    timing_ids = [timing.shot_id for timing in timings]
    if timing_ids != shot_ids:
        raise ValueError("Video timings must match shot IDs exactly and preserve order")

    frame_ids = [frame.shot_id for frame in storyboard_frames]
    if frame_ids != shot_ids:
        raise ValueError("Video storyboard frames must match shot IDs exactly and preserve order")

    previous_end: float | None = None
    for shot, timing in zip(shots, timings, strict=True):
        if timing.end_seconds <= timing.start_seconds:
            raise ValueError(f"Video shot {shot.id} must have a positive duration")
        if previous_end is not None and not math.isclose(
            timing.start_seconds,
            previous_end,
            abs_tol=1e-6,
        ):
            raise ValueError("Video shot timings must form a contiguous timeline")
        previous_end = timing.end_seconds
