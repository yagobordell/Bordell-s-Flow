import asyncio
from typing import Any

import pytest

from ai_video_factory.bots.storyboard_frames import (
    STORYBOARD_FRAME_INSTRUCTIONS,
    StoryboardFrameBot,
    StoryboardPromptOutput,
)
from ai_video_factory.domain import Shot, ShotTiming, VisualReference
from ai_video_factory.workflows.storyboard_frames import build_storyboard_frames


class FakeStructuredProvider:
    def __init__(self, prompts: list[str]) -> None:
        self.prompts = prompts
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(self, **kwargs: Any) -> StoryboardPromptOutput:
        self.calls.append(kwargs)
        return StoryboardPromptOutput(prompt=self.prompts[len(self.calls) - 1])


def _shots() -> list[Shot]:
    return [
        Shot(
            id=1,
            scene_id=1,
            beat_ids=[1],
            entity_ids=["group_001"],
            action="Samurai warriors stand as the ruling military class.",
        ),
        Shot(
            id=2,
            scene_id=1,
            beat_ids=[2],
            entity_ids=["group_001", "location_001"],
            action="The samurai emerge in a feudal Japanese setting.",
        ),
    ]


def _timings() -> list[ShotTiming]:
    return [
        ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=3.5),
        ShotTiming(shot_id=2, start_seconds=3.5, end_seconds=10.88),
    ]


def _references() -> list[VisualReference]:
    return [
        VisualReference(entity_id="group_001", prompt="Canonical samurai identity reference."),
        VisualReference(
            entity_id="location_001",
            prompt="Canonical feudal Japanese environment reference.",
        ),
    ]


def test_storyboard_instructions_require_action_visibility_and_visual_progression() -> None:
    expected_action_rule = "representar de forma visible el núcleo de `SHOT.action`"
    assert expected_action_rule in STORYBOARD_FRAME_INSTRUCTIONS
    assert "Continuidad no significa repetición" in STORYBOARD_FRAME_INSTRUCTIONS
    assert "variación visual significativa" in STORYBOARD_FRAME_INSTRUCTIONS
    assert "idea abstracta como legado, memoria, símbolo, influencia o mito" in (
        STORYBOARD_FRAME_INSTRUCTIONS
    )
    assert "evidencia visual concreta o una metáfora física" in STORYBOARD_FRAME_INSTRUCTIONS


def test_storyboard_workflow_is_serial_and_carries_previous_frame() -> None:
    provider = FakeStructuredProvider(["First keyframe", "Second keyframe"])
    bot = StoryboardFrameBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    frames = asyncio.run(
        build_storyboard_frames(
            _shots(),
            _timings(),
            _references(),
            frame_bot=bot,
            visual_style="cinematic documentary",
            aspect_ratio="9:16",
        )
    )

    assert [frame.model_dump() for frame in frames] == [
        {"shot_id": 1, "prompt": "First keyframe"},
        {"shot_id": 2, "prompt": "Second keyframe"},
    ]
    assert len(provider.calls) == 2
    assert "PREVIOUS STORYBOARD FRAME:\n(none)" in provider.calls[0]["input_text"]
    assert "PREVIOUS STORYBOARD FRAME:\nFirst keyframe" in provider.calls[1]["input_text"]


def test_storyboard_workflow_resets_previous_frame_on_new_scene() -> None:
    provider = FakeStructuredProvider(["First scene", "Second scene"])
    bot = StoryboardFrameBot(provider=provider, model="test-model")  # type: ignore[arg-type]
    shots = _shots()
    shots[1] = shots[1].model_copy(update={"scene_id": 2})

    asyncio.run(
        build_storyboard_frames(
            shots,
            _timings(),
            _references(),
            frame_bot=bot,
            visual_style="cinematic documentary",
            aspect_ratio="9:16",
        )
    )

    assert "PREVIOUS STORYBOARD FRAME:\n(none)" in provider.calls[0]["input_text"]
    assert "PREVIOUS STORYBOARD FRAME:\n(none)" in provider.calls[1]["input_text"]


def test_storyboard_bot_receives_duration_and_only_relevant_references() -> None:
    provider = FakeStructuredProvider(["Frame one", "Frame two"])
    bot = StoryboardFrameBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    asyncio.run(
        build_storyboard_frames(
            _shots(),
            _timings(),
            _references(),
            frame_bot=bot,
            visual_style="cinematic documentary",
            aspect_ratio="9:16",
        )
    )

    first_input = provider.calls[0]["input_text"]
    second_input = provider.calls[1]["input_text"]
    assert "duration_seconds: 3.500" in first_input
    assert "group_001: Canonical samurai identity reference." in first_input
    assert "location_001" not in first_input
    assert "duration_seconds: 7.380" in second_input
    assert "location_001: Canonical feudal Japanese environment reference." in second_input


def test_storyboard_workflow_rejects_unknown_visual_entity() -> None:
    provider = FakeStructuredProvider(["unused"])
    bot = StoryboardFrameBot(provider=provider, model="test-model")  # type: ignore[arg-type]
    shots = [
        Shot(
            id=1,
            scene_id=1,
            beat_ids=[1],
            entity_ids=["group_999"],
            action="Unknown group appears.",
        )
    ]

    with pytest.raises(ValueError, match="unknown visual entities"):
        asyncio.run(
            build_storyboard_frames(
                shots,
                [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=2.0)],
                _references(),
                frame_bot=bot,
                visual_style="cinematic documentary",
                aspect_ratio="9:16",
            )
        )

    assert provider.calls == []


def test_storyboard_workflow_rejects_noncontiguous_timings() -> None:
    provider = FakeStructuredProvider(["unused", "unused"])
    bot = StoryboardFrameBot(provider=provider, model="test-model")  # type: ignore[arg-type]
    timings = [
        ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=3.5),
        ShotTiming(shot_id=2, start_seconds=3.6, end_seconds=10.88),
    ]

    with pytest.raises(ValueError, match="contiguous timeline"):
        asyncio.run(
            build_storyboard_frames(
                _shots(),
                timings,
                _references(),
                frame_bot=bot,
                visual_style="cinematic documentary",
                aspect_ratio="9:16",
            )
        )

    assert provider.calls == []
