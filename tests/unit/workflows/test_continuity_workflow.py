import asyncio

from ai_video_factory.legacy_bots.continuity import ContinuityDecision, NewContinuityEntity
from ai_video_factory.domain import ContinuityEntity, NarrativeBlock
from ai_video_factory.providers.base import StatefulStructuredResult
from ai_video_factory.workflows.continuity import plan_continuity


class RecordingContinuityBot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, list[str], str | None]] = []

    async def run(
        self,
        block: NarrativeBlock,
        *,
        known_entities: list[ContinuityEntity],
        previous_response_id: str | None,
    ) -> StatefulStructuredResult[ContinuityDecision]:
        self.calls.append(
            (block.id, [entity.id for entity in known_entities], previous_response_id)
        )

        if block.id == 1:
            assert known_entities == []
            assert previous_response_id is None
            return StatefulStructuredResult(
                output=ContinuityDecision(
                    existing_entity_ids=[],
                    new_entities=[
                        NewContinuityEntity(
                            kind="group",
                            name="Samuráis",
                            description="Guerreros de la élite feudal japonesa",
                        ),
                        NewContinuityEntity(
                            kind="location",
                            name="Japón feudal",
                            description="Entorno histórico del Japón feudal",
                        ),
                    ],
                ),
                response_id="resp_1",
            )

        assert [entity.id for entity in known_entities] == ["group_001", "location_001"]
        assert previous_response_id == "resp_1"
        return StatefulStructuredResult(
            output=ContinuityDecision(
                existing_entity_ids=["group_001"],
                new_entities=[
                    NewContinuityEntity(
                        kind="object",
                        name="Katana",
                        description="Espada japonesa asociada al samurái",
                    )
                ],
            ),
            response_id="resp_2",
        )


def test_plan_continuity_is_serial_and_assigns_deterministic_entity_ids() -> None:
    bot = RecordingContinuityBot()
    blocks = [
        NarrativeBlock(id=1, text="Los samuráis sirven en el Japón feudal."),
        NarrativeBlock(id=2, text="Dominan la katana y mantienen su disciplina."),
    ]

    entities, continuity = asyncio.run(
        plan_continuity(blocks, continuity_bot=bot)  # type: ignore[arg-type]
    )

    assert bot.calls == [
        (1, [], None),
        (2, ["group_001", "location_001"], "resp_1"),
    ]
    assert [entity.model_dump() for entity in entities] == [
        {
            "id": "group_001",
            "kind": "group",
            "name": "Samuráis",
            "description": "Guerreros de la élite feudal japonesa",
        },
        {
            "id": "location_001",
            "kind": "location",
            "name": "Japón feudal",
            "description": "Entorno histórico del Japón feudal",
        },
        {
            "id": "object_001",
            "kind": "object",
            "name": "Katana",
            "description": "Espada japonesa asociada al samurái",
        },
    ]
    assert [item.model_dump() for item in continuity] == [
        {"block_id": 1, "entity_ids": ["group_001", "location_001"]},
        {"block_id": 2, "entity_ids": ["group_001", "object_001"]},
    ]
