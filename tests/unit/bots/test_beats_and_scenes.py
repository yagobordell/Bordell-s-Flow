import asyncio
from typing import Any

from pydantic import BaseModel

from ai_video_factory.domain import Beat, NarrativeBlock
from ai_video_factory.legacy_bots.beats import BeatActionsOutput, BeatExtractorBot
from ai_video_factory.legacy_bots.scenes import SceneGroupsOutput, ScenePlannerBot


class FakeStructuredProvider:
    def __init__(self, result: BaseModel) -> None:
        self.result = result
        self.last_call: dict[str, Any] | None = None

    async def generate_structured(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        output_type: type[BaseModel],
    ) -> BaseModel:
        self.last_call = {
            "model": model,
            "instructions": instructions,
            "input_text": input_text,
            "output_type": output_type,
        }
        return self.result


def test_beat_extractor_returns_actions_without_ids() -> None:
    provider = FakeStructuredProvider(
        BeatActionsOutput(actions=["El clan gana poder.", "La corte pierde influencia."])
    )
    bot = BeatExtractorBot(provider=provider, model="test-model")
    block = NarrativeBlock(id=3, text="El clan gana poder y la corte pierde influencia.")

    actions = asyncio.run(bot.run(block))

    assert actions == ["El clan gana poder.", "La corte pierde influencia."]
    assert provider.last_call is not None
    assert provider.last_call["output_type"] is BeatActionsOutput


def test_scene_planner_builds_scenes_from_existing_beat_ids() -> None:
    beats = [
        Beat(id=1, block_id=1, action="A"),
        Beat(id=2, block_id=1, action="B"),
        Beat(id=3, block_id=2, action="C"),
    ]
    provider = FakeStructuredProvider(SceneGroupsOutput(scenes=[[1, 2], [3]]))
    bot = ScenePlannerBot(provider=provider, model="test-model")

    scenes = asyncio.run(bot.run(beats))

    assert [scene.model_dump() for scene in scenes] == [
        {"id": 1, "beat_ids": [1, 2]},
        {"id": 2, "beat_ids": [3]},
    ]


def test_scene_planner_rejects_missing_or_reordered_beats() -> None:
    beats = [
        Beat(id=1, block_id=1, action="A"),
        Beat(id=2, block_id=1, action="B"),
        Beat(id=3, block_id=2, action="C"),
    ]
    provider = FakeStructuredProvider(SceneGroupsOutput(scenes=[[1, 3], [2]]))
    bot = ScenePlannerBot(provider=provider, model="test-model")

    try:
        asyncio.run(bot.run(beats))
    except ValueError as exc:
        assert "every beat exactly once and preserve order" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
