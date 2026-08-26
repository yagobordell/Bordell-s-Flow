import asyncio
from typing import Any

from ai_video_factory.bots.visual_references import (
    VisualDesignOutput,
    VisualReferenceBot,
)
from ai_video_factory.domain import (
    BlockContinuity,
    ContinuityEntity,
    NarrativeBlock,
    VisualReference,
)
from ai_video_factory.workflows.visual_references import build_visual_references


class FakeStructuredProvider:
    def __init__(self, output: VisualDesignOutput) -> None:
        self.output = output
        self.last_call: dict[str, Any] | None = None

    async def generate_structured(self, **kwargs: Any) -> VisualDesignOutput:
        self.last_call = kwargs
        return self.output


def test_visual_reference_bot_uses_context_and_normalizes_location_prompt() -> None:
    provider = FakeStructuredProvider(
        VisualDesignOutput(
            description=(
                "A weathered mountain temple built from dark timber and stone, with a broad "
                "central stairway and red wooden gates."
            )
        )
    )
    bot = VisualReferenceBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    reference = asyncio.run(
        bot.run(
            ContinuityEntity(
                id="location_001",
                kind="location",
                name="templo de montaña",
                description="Templo antiguo construido en una ladera montañosa",
            ),
            visual_style="cinematic documentary",
            narrative_context="Los guerreros llegan al templo durante una época feudal.",
        )
    )

    assert reference.entity_id == "location_001"
    assert reference.prompt.startswith(
        "Canonical location reference, cinematic documentary."
    )
    assert "weathered mountain temple" in reference.prompt
    assert "single coherent environment" in reference.prompt.lower()
    assert ".." not in reference.prompt
    assert "no text" in reference.prompt
    assert provider.last_call is not None
    assert provider.last_call["output_type"] is VisualDesignOutput
    assert "ENTITY ID: location_001" in provider.last_call["input_text"]
    assert "época feudal" in provider.last_call["input_text"]
    assert "no describas un catálogo" in provider.last_call["instructions"]


class ParallelReferenceBot:
    """The first entity can finish only after the second entity has started."""

    def __init__(self) -> None:
        self.started = 0
        self.all_started = asyncio.Event()
        self.contexts: dict[str, str] = {}

    async def run(
        self,
        entity: ContinuityEntity,
        *,
        visual_style: str,
        narrative_context: str,
    ) -> VisualReference:
        self.started += 1
        self.contexts[entity.id] = narrative_context
        if self.started == 2:
            self.all_started.set()

        await asyncio.wait_for(self.all_started.wait(), timeout=0.5)
        return VisualReference(
            entity_id=entity.id,
            prompt=f"{visual_style}: {entity.name}",
        )


def test_visual_reference_workflow_derives_context_in_parallel_and_preserves_order() -> None:
    entities = [
        ContinuityEntity(
            id="character_001",
            kind="character",
            name="Luna",
            description="Exploradora",
        ),
        ContinuityEntity(
            id="location_001",
            kind="location",
            name="cueva",
            description="Cueva junto a un río",
        ),
    ]
    blocks = [
        NarrativeBlock(id=1, text="Luna entra en una cueva junto a un río."),
        NarrativeBlock(id=2, text="Luna avanza por el interior de la misma cueva."),
    ]
    continuity = [
        BlockContinuity(block_id=1, entity_ids=["character_001", "location_001"]),
        BlockContinuity(block_id=2, entity_ids=["character_001", "location_001"]),
    ]
    bot = ParallelReferenceBot()

    references = asyncio.run(
        build_visual_references(
            entities,
            narrative_blocks=blocks,
            block_continuity=continuity,
            reference_bot=bot,  # type: ignore[arg-type]
            visual_style="cinematic documentary",
        )
    )

    assert [reference.entity_id for reference in references] == [
        "character_001",
        "location_001",
    ]
    assert bot.started == 2
    assert bot.contexts["location_001"] == (
        "Luna entra en una cueva junto a un río. "
        "Luna avanza por el interior de la misma cueva."
    )


def test_visual_reference_workflow_rejects_duplicate_entity_ids() -> None:
    entity = ContinuityEntity(
        id="object_001",
        kind="object",
        name="brújula",
        description="Brújula antigua",
    )

    try:
        asyncio.run(
            build_visual_references(
                [entity, entity],
                narrative_blocks=[],
                block_continuity=[],
                reference_bot=ParallelReferenceBot(),  # type: ignore[arg-type]
                visual_style="cinematic documentary",
            )
        )
    except ValueError as exc:
        assert "unique IDs" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_visual_reference_workflow_rejects_entities_without_narrative_context() -> None:
    entity = ContinuityEntity(
        id="location_001",
        kind="location",
        name="valle",
        description="Valle montañoso",
    )

    try:
        asyncio.run(
            build_visual_references(
                [entity],
                narrative_blocks=[NarrativeBlock(id=1, text="El relato comienza lejos.")],
                block_continuity=[BlockContinuity(block_id=1, entity_ids=[])],
                reference_bot=ParallelReferenceBot(),  # type: ignore[arg-type]
                visual_style="cinematic documentary",
            )
        )
    except ValueError as exc:
        assert "narrative context" in str(exc)
        assert "location_001" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
