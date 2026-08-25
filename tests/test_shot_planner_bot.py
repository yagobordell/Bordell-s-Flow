import asyncio
from typing import Any

from ai_video_factory.bots.shots import PlannedShot, ShotPlanOutput, ShotPlannerBot
from ai_video_factory.domain import Beat, ContinuityEntity, Scene
from ai_video_factory.providers.base import StatefulStructuredResult


class FakeStatefulProvider:
    def __init__(self, output: ShotPlanOutput) -> None:
        self.output = output
        self.last_call: dict[str, Any] | None = None

    async def generate_structured_stateful(self, **kwargs: Any) -> StatefulStructuredResult:
        self.last_call = kwargs
        return StatefulStructuredResult(output=self.output, response_id="resp_next")


def test_shot_planner_rejects_unavailable_entity_ids() -> None:
    provider = FakeStatefulProvider(
        ShotPlanOutput(
            shots=[
                PlannedShot(
                    beat_ids=[1],
                    entity_ids=["object_999"],
                    action="Luna mira un objeto.",
                )
            ]
        )
    )
    bot = ShotPlannerBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    try:
        asyncio.run(
            bot.run(
                Scene(id=1, beat_ids=[1]),
                beats=[Beat(id=1, block_id=1, action="Luna mira a su alrededor.")],
                available_entities=[
                    ContinuityEntity(
                        id="character_001",
                        kind="character",
                        name="Luna",
                        description="Exploradora",
                    )
                ],
                previous_response_id=None,
            )
        )
    except ValueError as exc:
        assert "unavailable entity IDs" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_shot_planner_rejects_changed_beat_coverage() -> None:
    provider = FakeStatefulProvider(
        ShotPlanOutput(
            shots=[
                PlannedShot(
                    beat_ids=[2, 1],
                    entity_ids=[],
                    action="Acción reordenada.",
                )
            ]
        )
    )
    bot = ShotPlannerBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    try:
        asyncio.run(
            bot.run(
                Scene(id=1, beat_ids=[1, 2]),
                beats=[
                    Beat(id=1, block_id=1, action="Primera acción."),
                    Beat(id=2, block_id=1, action="Segunda acción."),
                ],
                available_entities=[],
                previous_response_id="resp_previous",
            )
        )
    except ValueError as exc:
        assert "every scene beat exactly once" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
