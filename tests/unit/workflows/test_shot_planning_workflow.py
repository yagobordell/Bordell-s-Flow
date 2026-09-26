import asyncio

from ai_video_factory.domain import Beat, BlockContinuity, ContinuityEntity, Scene
from ai_video_factory.legacy_bots.shots import PlannedShot, ShotPlanOutput
from ai_video_factory.providers.base import StatefulStructuredResult
from ai_video_factory.workflows.shot_planning import plan_shots


class RecordingShotBot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, list[int], list[str], str | None]] = []

    async def run(
        self,
        scene: Scene,
        *,
        beats: list[Beat],
        available_entities: list[ContinuityEntity],
        previous_response_id: str | None,
    ) -> StatefulStructuredResult[ShotPlanOutput]:
        self.calls.append(
            (
                scene.id,
                [beat.id for beat in beats],
                [entity.id for entity in available_entities],
                previous_response_id,
            )
        )

        if scene.id == 1:
            return StatefulStructuredResult(
                output=ShotPlanOutput(
                    shots=[
                        PlannedShot(
                            beat_ids=[1],
                            entity_ids=["character_001", "location_001"],
                            action="Luna entra en la cueva.",
                        ),
                        PlannedShot(
                            beat_ids=[2],
                            entity_ids=["character_001"],
                            action="Luna avanza en la oscuridad.",
                        ),
                    ]
                ),
                response_id="shot_resp_1",
            )

        assert previous_response_id == "shot_resp_1"
        return StatefulStructuredResult(
            output=ShotPlanOutput(
                shots=[
                    PlannedShot(
                        beat_ids=[3, 4],
                        entity_ids=["character_001", "location_001", "object_001"],
                        action="Luna sale de la cueva llevando la brújula.",
                    )
                ]
            ),
            response_id="shot_resp_2",
        )


def test_plan_shots_is_serial_and_assigns_global_deterministic_ids() -> None:
    bot = RecordingShotBot()
    beats = [
        Beat(id=1, block_id=1, action="Luna entra en una cueva."),
        Beat(id=2, block_id=1, action="Luna avanza en la oscuridad."),
        Beat(id=3, block_id=2, action="Luna encuentra una brújula antigua."),
        Beat(id=4, block_id=3, action="Luna sale de la cueva con la brújula."),
    ]
    scenes = [
        Scene(id=1, beat_ids=[1, 2]),
        Scene(id=2, beat_ids=[3, 4]),
    ]
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
            name="cueva junto a un río",
            description="Cueva cercana a un río",
        ),
        ContinuityEntity(
            id="object_001",
            kind="object",
            name="brújula antigua",
            description="Brújula antigua",
        ),
    ]
    continuity = [
        BlockContinuity(block_id=1, entity_ids=["character_001", "location_001"]),
        BlockContinuity(block_id=2, entity_ids=["character_001", "object_001"]),
        BlockContinuity(
            block_id=3,
            entity_ids=["character_001", "location_001", "object_001"],
        ),
    ]

    shots = asyncio.run(
        plan_shots(
            scenes,
            beats=beats,
            entities=entities,
            block_continuity=continuity,
            shot_bot=bot,  # type: ignore[arg-type]
        )
    )

    assert bot.calls == [
        (1, [1, 2], ["character_001", "location_001"], None),
        (
            2,
            [3, 4],
            ["character_001", "location_001", "object_001"],
            "shot_resp_1",
        ),
    ]
    assert [shot.model_dump() for shot in shots] == [
        {
            "id": 1,
            "scene_id": 1,
            "beat_ids": [1],
            "entity_ids": ["character_001", "location_001"],
            "action": "Luna entra en la cueva.",
        },
        {
            "id": 2,
            "scene_id": 1,
            "beat_ids": [2],
            "entity_ids": ["character_001"],
            "action": "Luna avanza en la oscuridad.",
        },
        {
            "id": 3,
            "scene_id": 2,
            "beat_ids": [3, 4],
            "entity_ids": ["character_001", "location_001", "object_001"],
            "action": "Luna sale de la cueva llevando la brújula.",
        },
    ]
