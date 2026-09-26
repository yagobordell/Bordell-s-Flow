import asyncio
from typing import Any

import pytest

from ai_video_factory.domain import Shot, ShotTiming, VisualReference
from ai_video_factory.legacy_bots.storyboard_frames import (
    STORYBOARD_FRAME_INSTRUCTIONS,
    StoryboardFrameBot,
    StoryboardPromptOutput,
)
from ai_video_factory.providers.ideogram_caption import (
    IdeogramCaptionPlan,
    IdeogramStylePlan,
    render_ideogram_caption,
)
from ai_video_factory.workflows.storyboard_frames import build_storyboard_frames


def _plan(description: str) -> IdeogramCaptionPlan:
    return IdeogramCaptionPlan(
        high_level_description=description,
        style=IdeogramStylePlan(
            aesthetics="cinematic documentary realism",
            lighting="soft directional daylight",
            medium="documentary photograph",
            render_mode="photo",
            render_description="realistic 35mm photography",
        ),
        background="Historically grounded feudal Japanese environment.",
        elements=[],
    )


class FakeStructuredProvider:
    def __init__(self, prompts: list[IdeogramCaptionPlan]) -> None:
        self.prompts = prompts
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(self, **kwargs: Any) -> StoryboardPromptOutput:
        self.calls.append(kwargs)
        return self.prompts[len(self.calls) - 1]


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
    assert "no recibe las imágenes de referencia" in STORYBOARD_FRAME_INSTRUCTIONS
    assert "solo elementos de tipo objeto" in STORYBOARD_FRAME_INSTRUCTIONS
    assert "horizontal 16:9" in STORYBOARD_FRAME_INSTRUCTIONS
    assert "landscape 16:9" in STORYBOARD_FRAME_INSTRUCTIONS
    assert "retrato vertical" in STORYBOARD_FRAME_INSTRUCTIONS
    assert "foreground, midground y background" in STORYBOARD_FRAME_INSTRUCTIONS
    assert "parallax" in STORYBOARD_FRAME_INSTRUCTIONS
    assert "documental vertical" not in STORYBOARD_FRAME_INSTRUCTIONS


def test_storyboard_workflow_is_serial_and_carries_previous_frame() -> None:
    first = _plan("First keyframe")
    second = _plan("Second keyframe")
    provider = FakeStructuredProvider([first, second])
    bot = StoryboardFrameBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    frames = asyncio.run(
        build_storyboard_frames(
            _shots(),
            _timings(),
            _references(),
            frame_bot=bot,
            visual_style="cinematic documentary",
            aspect_ratio="16:9",
        )
    )

    first_caption = render_ideogram_caption(first)
    second_caption = render_ideogram_caption(second)
    assert [frame.model_dump() for frame in frames] == [
        {"shot_id": 1, "prompt": first_caption},
        {"shot_id": 2, "prompt": second_caption},
    ]
    assert len(provider.calls) == 2
    assert "PREVIOUS STORYBOARD FRAME (Ideogram JSON caption):\n(none)" in (
        provider.calls[0]["input_text"]
    )
    assert (
        "PREVIOUS STORYBOARD FRAME (Ideogram JSON caption):\n" + first_caption
        in provider.calls[1]["input_text"]
    )
    assert provider.calls[0]["output_type"] is StoryboardPromptOutput


def test_storyboard_workflow_resets_previous_frame_on_new_scene() -> None:
    provider = FakeStructuredProvider([_plan("First scene"), _plan("Second scene")])
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
            aspect_ratio="16:9",
        )
    )

    previous_none = "PREVIOUS STORYBOARD FRAME (Ideogram JSON caption):\n(none)"
    assert previous_none in provider.calls[0]["input_text"]
    assert previous_none in provider.calls[1]["input_text"]


def test_storyboard_bot_receives_duration_and_only_relevant_references() -> None:
    provider = FakeStructuredProvider([_plan("Frame one"), _plan("Frame two")])
    bot = StoryboardFrameBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    asyncio.run(
        build_storyboard_frames(
            _shots(),
            _timings(),
            _references(),
            frame_bot=bot,
            visual_style="cinematic documentary",
            aspect_ratio="16:9",
        )
    )

    first_input = provider.calls[0]["input_text"]
    second_input = provider.calls[1]["input_text"]
    assert "duration_seconds: 3.500" in first_input
    assert "group_001: Canonical samurai identity reference." in first_input
    assert "location_001" not in first_input
    assert "duration_seconds: 7.380" in second_input
    assert "location_001: Canonical feudal Japanese environment reference." in second_input


def test_storyboard_bot_rejects_non_landscape_aspect_ratio() -> None:
    provider = FakeStructuredProvider([_plan("unused")])
    bot = StoryboardFrameBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="exactly 16:9"):
        asyncio.run(
            bot.run(
                _shots()[0],
                _timings()[0],
                [_references()[0]],
                visual_style="cinematic documentary",
                aspect_ratio="9:16",
                previous_frame=None,
            )
        )

    assert provider.calls == []


def test_storyboard_workflow_rejects_unknown_visual_entity() -> None:
    provider = FakeStructuredProvider([_plan("unused")])
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
                aspect_ratio="16:9",
            )
        )

    assert provider.calls == []


def test_storyboard_workflow_rejects_noncontiguous_timings() -> None:
    provider = FakeStructuredProvider([_plan("unused"), _plan("unused")])
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
                aspect_ratio="16:9",
            )
        )

    assert provider.calls == []
