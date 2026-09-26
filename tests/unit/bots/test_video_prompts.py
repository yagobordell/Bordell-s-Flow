import asyncio
from typing import Any

import pytest

from ai_video_factory.domain import Shot, ShotTiming, StoryboardFrame
from ai_video_factory.legacy_bots.video_prompts import (
    VIDEO_PROMPT_INSTRUCTIONS,
    VideoPromptBot,
    VideoPromptOutput,
)
from ai_video_factory.workflows.video_prompts import build_video_prompts


class FakeStructuredProvider:
    def __init__(self, prompts: list[str]) -> None:
        self.prompts = prompts
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(self, **kwargs: Any) -> VideoPromptOutput:
        self.calls.append(kwargs)
        return VideoPromptOutput(prompt=self.prompts[len(self.calls) - 1])


def _shots() -> list[Shot]:
    return [
        Shot(
            id=1,
            scene_id=1,
            beat_ids=[1],
            entity_ids=["group_001"],
            action="Samurai warriors hold formation as wind moves through their banners.",
        ),
        Shot(
            id=2,
            scene_id=1,
            beat_ids=[2],
            entity_ids=["group_001", "location_001"],
            action="The formation advances through the feudal settlement.",
        ),
    ]


def _timings() -> list[ShotTiming]:
    return [
        ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=3.5),
        ShotTiming(shot_id=2, start_seconds=3.5, end_seconds=8.0),
    ]


def _storyboard_frames() -> list[StoryboardFrame]:
    return [
        StoryboardFrame(
            shot_id=1,
            prompt="Samurai formation in a windswept field, banners visible behind them.",
        ),
        StoryboardFrame(
            shot_id=2,
            prompt="The same formation at the edge of a compact feudal settlement.",
        ),
    ]


def test_video_prompt_instructions_define_motion_only_single_shot_boundary() -> None:
    assert "movimiento del sujeto, del entorno y de cámara" in VIDEO_PROMPT_INSTRUCTIONS
    assert "un solo plano continuo" in VIDEO_PROMPT_INSTRUCTIONS
    assert "No añadas cortes, transiciones, montajes" in VIDEO_PROMPT_INSTRUCTIONS
    assert "No describas diálogo, voz en off, música ni diseño sonoro" in (
        VIDEO_PROMPT_INSTRUCTIONS
    )
    assert "no menciones LTX, seeds, frames, FPS" in VIDEO_PROMPT_INSTRUCTIONS
    assert "horizontal 16:9" in VIDEO_PROMPT_INSTRUCTIONS
    assert "landscape 16:9" in VIDEO_PROMPT_INSTRUCTIONS
    assert "slow cinematic push-in" in VIDEO_PROMPT_INSTRUCTIONS
    assert "lateral tracking" in VIDEO_PROMPT_INSTRUCTIONS
    assert "parallax" in VIDEO_PROMPT_INSTRUCTIONS
    assert "movimiento de cámara contenido" in VIDEO_PROMPT_INSTRUCTIONS
    assert "documental vertical" not in VIDEO_PROMPT_INSTRUCTIONS


def test_video_prompt_workflow_is_serial_and_carries_previous_prompt() -> None:
    provider = FakeStructuredProvider(["First motion", "Second motion"])
    bot = VideoPromptBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    prompts = asyncio.run(
        build_video_prompts(
            _shots(),
            _timings(),
            _storyboard_frames(),
            prompt_bot=bot,
            visual_style="cinematic documentary",
            aspect_ratio="16:9",
        )
    )

    assert [prompt.model_dump() for prompt in prompts] == [
        {"shot_id": 1, "prompt": "First motion"},
        {"shot_id": 2, "prompt": "Second motion"},
    ]
    assert len(provider.calls) == 2
    assert "PREVIOUS VIDEO PROMPT:\n(none)" in provider.calls[0]["input_text"]
    assert "PREVIOUS VIDEO PROMPT:\nFirst motion" in provider.calls[1]["input_text"]


def test_video_prompt_workflow_resets_previous_prompt_on_new_scene() -> None:
    provider = FakeStructuredProvider(["First scene motion", "Second scene motion"])
    bot = VideoPromptBot(provider=provider, model="test-model")  # type: ignore[arg-type]
    shots = _shots()
    shots[1] = shots[1].model_copy(update={"scene_id": 2})

    asyncio.run(
        build_video_prompts(
            shots,
            _timings(),
            _storyboard_frames(),
            prompt_bot=bot,
            visual_style="cinematic documentary",
            aspect_ratio="16:9",
        )
    )

    assert "PREVIOUS VIDEO PROMPT:\n(none)" in provider.calls[0]["input_text"]
    assert "PREVIOUS VIDEO PROMPT:\n(none)" in provider.calls[1]["input_text"]


def test_video_prompt_bot_receives_action_duration_and_starting_storyboard() -> None:
    provider = FakeStructuredProvider(["First motion", "Second motion"])
    bot = VideoPromptBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    asyncio.run(
        build_video_prompts(
            _shots(),
            _timings(),
            _storyboard_frames(),
            prompt_bot=bot,
            visual_style="cinematic documentary",
            aspect_ratio="16:9",
        )
    )

    first_input = provider.calls[0]["input_text"]
    assert "duration_seconds: 3.500" in first_input
    assert "Samurai warriors hold formation" in first_input
    assert "STARTING STORYBOARD KEYFRAME:\nSamurai formation" in first_input
    assert "visual_style: cinematic documentary" in first_input
    assert "aspect_ratio: 16:9" in first_input


def test_video_prompt_bot_rejects_non_landscape_aspect_ratio() -> None:
    provider = FakeStructuredProvider(["unused"])
    bot = VideoPromptBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="exactly 16:9"):
        asyncio.run(
            bot.run(
                _shots()[0],
                _timings()[0],
                _storyboard_frames()[0],
                visual_style="cinematic documentary",
                aspect_ratio="9:16",
                previous_prompt=None,
            )
        )

    assert provider.calls == []


def test_video_prompt_workflow_rejects_misaligned_storyboard_frames() -> None:
    provider = FakeStructuredProvider(["unused", "unused"])
    bot = VideoPromptBot(provider=provider, model="test-model")  # type: ignore[arg-type]
    frames = _storyboard_frames()
    frames[1] = frames[1].model_copy(update={"shot_id": 3})

    with pytest.raises(ValueError, match="storyboard frames must match shot IDs exactly"):
        asyncio.run(
            build_video_prompts(
                _shots(),
                _timings(),
                frames,
                prompt_bot=bot,
                visual_style="cinematic documentary",
                aspect_ratio="16:9",
            )
        )

    assert provider.calls == []


def test_video_prompt_workflow_rejects_noncontiguous_timings() -> None:
    provider = FakeStructuredProvider(["unused", "unused"])
    bot = VideoPromptBot(provider=provider, model="test-model")  # type: ignore[arg-type]
    timings = [
        ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=3.5),
        ShotTiming(shot_id=2, start_seconds=3.6, end_seconds=8.0),
    ]

    with pytest.raises(ValueError, match="contiguous timeline"):
        asyncio.run(
            build_video_prompts(
                _shots(),
                timings,
                _storyboard_frames(),
                prompt_bot=bot,
                visual_style="cinematic documentary",
                aspect_ratio="16:9",
            )
        )

    assert provider.calls == []


def test_video_prompt_bot_rejects_mismatched_storyboard_id() -> None:
    provider = FakeStructuredProvider(["unused"])
    bot = VideoPromptBot(provider=provider, model="test-model")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="matching shot and storyboard frame IDs"):
        asyncio.run(
            bot.run(
                _shots()[0],
                _timings()[0],
                StoryboardFrame(shot_id=2, prompt="Wrong frame"),
                visual_style="cinematic documentary",
                aspect_ratio="16:9",
                previous_prompt=None,
            )
        )

    assert provider.calls == []
