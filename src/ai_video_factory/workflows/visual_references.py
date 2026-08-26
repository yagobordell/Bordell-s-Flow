import asyncio

from ai_video_factory.bots.visual_references import VisualReferenceBot
from ai_video_factory.domain import ContinuityEntity, VisualReference


async def build_visual_references(
    entities: list[ContinuityEntity],
    *,
    reference_bot: VisualReferenceBot,
    visual_style: str,
) -> list[VisualReference]:
    """Build canonical visual references independently while preserving entity order."""

    entity_ids = [entity.id for entity in entities]
    if len(entity_ids) != len(set(entity_ids)):
        raise ValueError("Continuity entities must have unique IDs")

    if not entities:
        return []

    references = await asyncio.gather(
        *(
            reference_bot.run(entity, visual_style=visual_style)
            for entity in entities
        )
    )

    returned_ids = [reference.entity_id for reference in references]
    if returned_ids != entity_ids:
        raise ValueError(
            "Visual reference workflow must return exactly one reference per entity in input order"
        )

    return list(references)
