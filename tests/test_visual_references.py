import asyncio
from typing import Any

from ai_video_factory.bots.visual_references import (
    VisualDesignOutput,
    VisualReferenceBot,
)
from ai_video_factory.domain import ContinuityEntity, VisualReference
from ai_video_factory.workflows.visual_references import build_visual_references


class FakeStructuredProvider:
    def __init__(self, output: VisualDesignOutput) -> None:
        self.output = output
        self.last_call: dict[str, Any] | None = None

    async def generate_structured(self, **kwargs: Any) -> VisualDesignOutput:
        self.last_call = kwargs
        return self.output


def test_visual_reference_bot_applies_fixed_location_template() -> None:
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
        )
    )

    assert reference.entity_id == "location_001"
    assert reference.prompt.startswith(
        "Canonical location reference, cinematic documentary."
    )
    assert "weathered mountain temple" in reference.prompt
    assert "permanent architecture" in reference.prompt
    assert "no text" in reference.prompt
    assert provider.last_call is not None
    assert provider.last_call["output_type"] is VisualDesignOutput
    assert "ENTITY ID: location_001" in provider.last_call["input_text"]


class ParallelReferenceBot:
    """The first entity can finish only after the second entity has started."""

    def __init__(self) -> None:
        self.started = 0
        self.all_started = asyncio.Event()

    async def run(
        self,
        entity: ContinuityEntity,
        *,
        visual_style: str,
    ) -> VisualReference:
        self.started += 1
        if self.started == 2:
            self.all_started.set()

        await asyncio.wait_for(self.all_started.wait(), timeout=0.5)
        return VisualReference(
            entity_id=entity.id,
            prompt=f"{visual_style}: {entity.name}",
        )


def test_visual_reference_workflow_runs_entities_in_parallel_and_preserves_order() -> None:
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
    bot = ParallelReferenceBot()

    references = asyncio.run(
        build_visual_references(
            entities,
            reference_bot=bot,  # type: ignore[arg-type]
            visual_style="cinematic documentary",
        )
    )

    assert [reference.entity_id for reference in references] == [
        "character_001",
        "location_001",
    ]
    assert bot.started == 2


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
                reference_bot=ParallelReferenceBot(),  # type: ignore[arg-type]
                visual_style="cinematic documentary",
            )
        )
    except ValueError as exc:
        assert "unique IDs" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
