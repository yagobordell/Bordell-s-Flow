import asyncio
import math

from ai_video_factory.domain import Shot, ShotTiming, StoryboardFrame, VideoPrompt
from ai_video_factory.legacy_bots.video_prompts import VideoPromptBot


async def build_video_prompts(
    shots: list[Shot],
    timings: list[ShotTiming],
    storyboard_frames: list[StoryboardFrame],
    *,
    prompt_bot: VideoPromptBot,
    visual_style: str,
    aspect_ratio: str,
    max_scene_concurrency: int = 3,
) -> list[VideoPrompt]:
    """Build ordered motion prompts while carrying explicit within-scene temporal context."""

    _validate_inputs(shots, timings, storyboard_frames)
    if max_scene_concurrency < 1:
        raise ValueError("Video prompt max_scene_concurrency must be at least 1")

    scene_groups = _group_scene_inputs(shots, timings, storyboard_frames)
    gate = asyncio.Semaphore(max_scene_concurrency)

    async def build_scene(
        scene_inputs: list[tuple[Shot, ShotTiming, StoryboardFrame]],
    ) -> list[VideoPrompt]:
        async with gate:
            prompts: list[VideoPrompt] = []
            previous_prompt: VideoPrompt | None = None
            for shot, timing, storyboard_frame in scene_inputs:
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
            return prompts

    grouped_prompts = await asyncio.gather(*(build_scene(group) for group in scene_groups))
    return [prompt for group in grouped_prompts for prompt in group]


def _group_scene_inputs(
    shots: list[Shot],
    timings: list[ShotTiming],
    storyboard_frames: list[StoryboardFrame],
) -> list[list[tuple[Shot, ShotTiming, StoryboardFrame]]]:
    groups: list[list[tuple[Shot, ShotTiming, StoryboardFrame]]] = []
    for shot, timing, storyboard_frame in zip(
        shots,
        timings,
        storyboard_frames,
        strict=True,
    ):
        if not groups or groups[-1][0][0].scene_id != shot.scene_id:
            groups.append([])
        groups[-1].append((shot, timing, storyboard_frame))
    return groups


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
