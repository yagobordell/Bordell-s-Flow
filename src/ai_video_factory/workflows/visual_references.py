import asyncio

from ai_video_factory.bots.visual_references import VisualReferenceBot
from ai_video_factory.domain import (
    BlockContinuity,
    ContinuityEntity,
    NarrativeBlock,
    VisualReference,
)


async def build_visual_references(
    entities: list[ContinuityEntity],
    *,
    narrative_blocks: list[NarrativeBlock],
    block_continuity: list[BlockContinuity],
    reference_bot: VisualReferenceBot,
    visual_style: str,
    aspect_ratio: str = "16:9",
) -> list[VisualReference]:
    """Build contextual visual references independently while preserving entity order."""

    if aspect_ratio.strip() != "16:9":
        raise ValueError("Visual references production aspect ratio must be exactly 16:9")

    entity_ids = [entity.id for entity in entities]
    if len(entity_ids) != len(set(entity_ids)):
        raise ValueError("Continuity entities must have unique IDs")

    block_by_id = {block.id: block for block in narrative_blocks}
    if len(block_by_id) != len(narrative_blocks):
        raise ValueError("Narrative blocks must have unique IDs")

    continuity_by_block = {item.block_id: item for item in block_continuity}
    if len(continuity_by_block) != len(block_continuity):
        raise ValueError("Block continuity entries must have unique block IDs")

    known_entity_ids = set(entity_ids)
    for item in block_continuity:
        if item.block_id not in block_by_id:
            raise ValueError(
                f"Block continuity referenced unknown narrative block ID: {item.block_id}"
            )
        unknown_ids = [
            entity_id for entity_id in item.entity_ids if entity_id not in known_entity_ids
        ]
        if unknown_ids:
            raise ValueError(
                "Block continuity referenced unknown entity IDs: " + ", ".join(unknown_ids)
            )

    if not entities:
        return []

    contexts: dict[str, list[str]] = {entity_id: [] for entity_id in entity_ids}
    for block in narrative_blocks:
        continuity = continuity_by_block.get(block.id)
        if continuity is None:
            continue
        for entity_id in continuity.entity_ids:
            contexts[entity_id].append(block.text)

    missing_context_ids = [entity_id for entity_id, texts in contexts.items() if not texts]
    if missing_context_ids:
        raise ValueError(
            "Visual references require narrative context for every entity: "
            + ", ".join(missing_context_ids)
        )

    references = await asyncio.gather(
        *(
            reference_bot.run(
                entity,
                visual_style=visual_style,
                narrative_context=" ".join(contexts[entity.id]),
                aspect_ratio=aspect_ratio,
            )
            for entity in entities
        )
    )

    returned_ids = [reference.entity_id for reference in references]
    if returned_ids != entity_ids:
        raise ValueError(
            "Visual reference workflow must return exactly one reference per entity in input order"
        )

    return list(references)
