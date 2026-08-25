import asyncio
from typing import Any

from ai_video_factory.bots.continuity import ContinuityBot, ContinuityDecision
from ai_video_factory.domain import ContinuityEntity, NarrativeBlock
from ai_video_factory.providers.base import StatefulStructuredResult


class FakeStatefulProvider:
    def __init__(self, decision: ContinuityDecision) -> None:
        self.decision = decision
        self.last_call: dict[str, Any] | None = None

    async def generate_structured_stateful(self, **kwargs: Any) -> StatefulStructuredResult:
        self.last_call = kwargs
        return StatefulStructuredResult(output=self.decision, response_id="resp_next")


def test_continuity_bot_rejects_unknown_existing_entity_ids() -> None:
    provider = FakeStatefulProvider(
        ContinuityDecision(existing_entity_ids=["character_999"], new_entities=[])
    )
    bot = ContinuityBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    try:
        asyncio.run(
            bot.run(
                NarrativeBlock(id=2, text="El guerrero reaparece."),
                known_entities=[
                    ContinuityEntity(
                        id="character_001",
                        kind="character",
                        name="Guerrero",
                        description="Guerrero mencionado anteriormente",
                    )
                ],
                previous_response_id="resp_previous",
            )
        )
    except ValueError as exc:
        assert "unknown entity IDs" in str(exc)
    else:
        raise AssertionError("Expected ValueError")

    assert provider.last_call is not None
    assert provider.last_call["previous_response_id"] == "resp_previous"
    assert provider.last_call["output_type"] is ContinuityDecision
