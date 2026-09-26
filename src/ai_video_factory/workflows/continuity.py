from ai_video_factory.domain import BlockContinuity, ContinuityEntity, NarrativeBlock
from ai_video_factory.legacy_bots.continuity import ContinuityBot


async def plan_continuity(
    blocks: list[NarrativeBlock],
    *,
    continuity_bot: ContinuityBot,
) -> tuple[list[ContinuityEntity], list[BlockContinuity]]:
    """Process narrative blocks serially and build a deterministic continuity registry."""

    block_ids = [block.id for block in blocks]
    if block_ids != sorted(set(block_ids)):
        raise ValueError("Narrative blocks must have unique IDs in ascending order")

    entities: list[ContinuityEntity] = []
    block_continuity: list[BlockContinuity] = []
    previous_response_id: str | None = None
    counters = {
        "character": 0,
        "group": 0,
        "location": 0,
        "object": 0,
    }

    for block in blocks:
        step = await continuity_bot.run(
            block,
            known_entities=entities,
            previous_response_id=previous_response_id,
        )

        entity_ids = list(step.output.existing_entity_ids)
        for candidate in step.output.new_entities:
            counters[candidate.kind] += 1
            entity_id = f"{candidate.kind}_{counters[candidate.kind]:03d}"
            entity = ContinuityEntity(
                id=entity_id,
                kind=candidate.kind,
                name=candidate.name.strip(),
                description=candidate.description.strip(),
            )
            entities.append(entity)
            entity_ids.append(entity.id)

        block_continuity.append(
            BlockContinuity(block_id=block.id, entity_ids=entity_ids)
        )
        previous_response_id = step.response_id

    return entities, block_continuity
