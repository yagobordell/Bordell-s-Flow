from ai_video_factory.bots.shots import ShotPlannerBot
from ai_video_factory.domain import Beat, BlockContinuity, ContinuityEntity, Scene, Shot


async def plan_shots(
    scenes: list[Scene],
    *,
    beats: list[Beat],
    entities: list[ContinuityEntity],
    block_continuity: list[BlockContinuity],
    shot_bot: ShotPlannerBot,
) -> list[Shot]:
    """Plan scenes serially while preserving canonical beats and continuity entities."""

    scene_ids = [scene.id for scene in scenes]
    if scene_ids != sorted(set(scene_ids)):
        raise ValueError("Scenes must have unique IDs in ascending order")

    beat_by_id = {beat.id: beat for beat in beats}
    if len(beat_by_id) != len(beats):
        raise ValueError("Beats must have unique IDs")

    entity_by_id = {entity.id: entity for entity in entities}
    if len(entity_by_id) != len(entities):
        raise ValueError("Continuity entities must have unique IDs")

    continuity_by_block = {item.block_id: item for item in block_continuity}
    if len(continuity_by_block) != len(block_continuity):
        raise ValueError("Block continuity entries must have unique block IDs")

    known_entity_ids = set(entity_by_id)
    for item in block_continuity:
        if len(item.entity_ids) != len(set(item.entity_ids)):
            raise ValueError(
                f"Block continuity for block {item.block_id} contains duplicate entity IDs"
            )
        unknown_ids = [
            entity_id
            for entity_id in item.entity_ids
            if entity_id not in known_entity_ids
        ]
        if unknown_ids:
            raise ValueError(
                "Block continuity referenced unknown entity IDs: " + ", ".join(unknown_ids)
            )

    shots: list[Shot] = []
    previous_response_id: str | None = None
    next_shot_id = 1

    for scene in scenes:
        try:
            scene_beats = [beat_by_id[beat_id] for beat_id in scene.beat_ids]
        except KeyError as exc:
            raise ValueError(
                f"Scene {scene.id} referenced unknown beat ID: {exc.args[0]}"
            ) from exc

        block_ids = list(dict.fromkeys(beat.block_id for beat in scene_beats))
        missing_blocks = [
            block_id for block_id in block_ids if block_id not in continuity_by_block
        ]
        if missing_blocks:
            raise ValueError(
                "Missing continuity for narrative block IDs: "
                + ", ".join(str(block_id) for block_id in missing_blocks)
            )

        available_ids: set[str] = set()
        for block_id in block_ids:
            available_ids.update(continuity_by_block[block_id].entity_ids)

        available_entities = [
            entity for entity in entities if entity.id in available_ids
        ]

        step = await shot_bot.run(
            scene,
            beats=scene_beats,
            available_entities=available_entities,
            previous_response_id=previous_response_id,
        )

        for planned in step.output.shots:
            shots.append(
                Shot(
                    id=next_shot_id,
                    scene_id=scene.id,
                    beat_ids=planned.beat_ids,
                    entity_ids=planned.entity_ids,
                    action=planned.action.strip(),
                )
            )
            next_shot_id += 1

        previous_response_id = step.response_id

    return shots
