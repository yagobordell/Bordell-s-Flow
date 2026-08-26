import math

from ai_video_factory.bots.storyboard_frames import StoryboardFrameBot
from ai_video_factory.domain import Shot, ShotTiming, StoryboardFrame, VisualReference


async def build_storyboard_frames(
    shots: list[Shot],
    timings: list[ShotTiming],
    references: list[VisualReference],
    *,
    frame_bot: StoryboardFrameBot,
    visual_style: str,
    aspect_ratio: str,
) -> list[StoryboardFrame]:
    """Build ordered storyboard keyframe prompts while carrying explicit visual context."""

    _validate_inputs(shots, timings, references)
    references_by_id = {reference.entity_id: reference for reference in references}

    frames: list[StoryboardFrame] = []
    previous_frame: StoryboardFrame | None = None
    previous_scene_id: int | None = None

    for shot, timing in zip(shots, timings, strict=True):
        if previous_scene_id is not None and shot.scene_id != previous_scene_id:
            previous_frame = None

        shot_references = [references_by_id[entity_id] for entity_id in shot.entity_ids]
        prompt = await frame_bot.run(
            shot,
            timing,
            shot_references,
            visual_style=visual_style,
            aspect_ratio=aspect_ratio,
            previous_frame=previous_frame,
        )
        if not prompt.strip():
            raise ValueError("Storyboard frame bot returned an empty prompt")

        frame = StoryboardFrame(shot_id=shot.id, prompt=prompt.strip())
        frames.append(frame)
        previous_frame = frame
        previous_scene_id = shot.scene_id

    return frames


def _validate_inputs(
    shots: list[Shot],
    timings: list[ShotTiming],
    references: list[VisualReference],
) -> None:
    if not shots:
        raise ValueError("Storyboard planning requires at least one shot")

    shot_ids = [shot.id for shot in shots]
    if shot_ids != list(range(1, len(shots) + 1)):
        raise ValueError("Storyboard shots must have consecutive IDs starting at 1")

    scene_ids = [shot.scene_id for shot in shots]
    if scene_ids != sorted(scene_ids):
        raise ValueError("Storyboard shots must preserve non-decreasing scene order")

    timing_ids = [timing.shot_id for timing in timings]
    if timing_ids != shot_ids:
        raise ValueError("Storyboard timings must match shot IDs exactly and preserve order")

    reference_ids = [reference.entity_id for reference in references]
    if len(reference_ids) != len(set(reference_ids)):
        raise ValueError("Storyboard visual references must have unique entity IDs")
    reference_id_set = set(reference_ids)

    previous_end: float | None = None
    for shot, timing in zip(shots, timings, strict=True):
        if timing.end_seconds <= timing.start_seconds:
            raise ValueError(f"Storyboard shot {shot.id} must have a positive duration")
        if previous_end is not None and not math.isclose(
            timing.start_seconds,
            previous_end,
            abs_tol=1e-6,
        ):
            raise ValueError("Storyboard shot timings must form a contiguous timeline")
        previous_end = timing.end_seconds

        if len(shot.entity_ids) != len(set(shot.entity_ids)):
            raise ValueError(f"Storyboard shot {shot.id} contains duplicate entity IDs")

        unknown_entity_ids = [
            entity_id for entity_id in shot.entity_ids if entity_id not in reference_id_set
        ]
        if unknown_entity_ids:
            raise ValueError(
                f"Storyboard shot {shot.id} references unknown visual entities: "
                f"{unknown_entity_ids}"
            )
