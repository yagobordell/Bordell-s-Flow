import asyncio

from ai_video_factory.domain import Beat, NarrativeBlock, Scene, SourceScript
from ai_video_factory.legacy_bots import BeatExtractorBot, NarrativeBlockBot, ScenePlannerBot


async def plan_narrative(
    source: SourceScript,
    *,
    block_bot: NarrativeBlockBot,
    beat_bot: BeatExtractorBot,
    scene_bot: ScenePlannerBot,
) -> tuple[list[NarrativeBlock], list[Beat], list[Scene]]:
    """Plan a script as blocks -> beats -> scenes.

    Beat extraction fans out across narrative blocks and is gathered back in original block order.
    IDs are assigned by application code after the parallel work completes, making them
    deterministic.
    """

    blocks = await block_bot.run(source)

    action_groups = await asyncio.gather(*(beat_bot.run(block) for block in blocks))

    beats: list[Beat] = []
    for block, actions in zip(blocks, action_groups, strict=True):
        for action in actions:
            beats.append(
                Beat(
                    id=len(beats) + 1,
                    block_id=block.id,
                    action=action,
                )
            )

    scenes = await scene_bot.run(beats)
    return blocks, beats, scenes
