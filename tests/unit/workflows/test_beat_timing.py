import asyncio
from typing import Any

import pytest

from ai_video_factory.legacy_bots.beat_timing import BeatTimingBot, BeatWordBoundariesOutput
from ai_video_factory.domain import Beat, NarrationAudio, NarrationWord, SourceScript
from ai_video_factory.workflows.beat_timing import build_beat_timings


class FakeStructuredProvider:
    def __init__(self, boundaries: list[int]) -> None:
        self.boundaries = boundaries
        self.last_call: dict[str, Any] | None = None

    async def generate_structured(self, **kwargs: Any) -> BeatWordBoundariesOutput:
        self.last_call = kwargs
        return BeatWordBoundariesOutput(beat_end_word_ids=self.boundaries)


def _words() -> list[NarrationWord]:
    return [
        NarrationWord(id=1, text="Durante", start_seconds=0.0, end_seconds=0.4),
        NarrationWord(id=2, text="siglos", start_seconds=0.4, end_seconds=0.8),
        NarrationWord(id=3, text="Japón", start_seconds=1.0, end_seconds=1.3),
        NarrationWord(id=4, text="cambió", start_seconds=1.3, end_seconds=1.7),
    ]


def _beats() -> list[Beat]:
    return [
        Beat(id=1, block_id=1, action="Japón atraviesa siglos de historia."),
        Beat(id=2, block_id=1, action="El país cambia con el tiempo."),
    ]


def test_beat_timing_bot_returns_semantic_word_boundaries() -> None:
    provider = FakeStructuredProvider([2, 4])
    bot = BeatTimingBot(provider=provider, model="test-model")  # type: ignore[arg-type]
    source = SourceScript(text="Durante siglos, Japón cambió.")

    boundaries = asyncio.run(bot.run(source, _beats(), _words()))

    assert boundaries == [2, 4]
    assert provider.last_call is not None
    assert provider.last_call["output_type"] is BeatWordBoundariesOutput
    assert "GUION CANÓNICO" in provider.last_call["input_text"]
    assert "1: [0.000-0.400] Durante" in provider.last_call["input_text"]
    assert "2: El país cambia con el tiempo." in provider.last_call["input_text"]


class FakeTimingBot:
    def __init__(self, boundaries: list[int]) -> None:
        self.boundaries = boundaries

    async def run(
        self,
        source: SourceScript,
        beats: list[Beat],
        words: list[NarrationWord],
    ) -> list[int]:
        return self.boundaries


def test_beat_timing_workflow_builds_contiguous_full_duration_intervals() -> None:
    timings = asyncio.run(
        build_beat_timings(
            SourceScript(text="Durante siglos, Japón cambió."),
            _beats(),
            _words(),
            NarrationAudio(uri="narration.wav", duration_seconds=2.0),
            timing_bot=FakeTimingBot([2, 4]),  # type: ignore[arg-type]
        )
    )

    assert [timing.model_dump() for timing in timings] == [
        {
            "beat_id": 1,
            "start_word_id": 1,
            "end_word_id": 2,
            "start_seconds": 0.0,
            "end_seconds": 1.0,
        },
        {
            "beat_id": 2,
            "start_word_id": 3,
            "end_word_id": 4,
            "start_seconds": 1.0,
            "end_seconds": 2.0,
        },
    ]


def test_beat_timing_workflow_rejects_nonconsecutive_word_ids() -> None:
    words = [
        NarrationWord(id=1, text="uno", start_seconds=0.0, end_seconds=0.3),
        NarrationWord(id=3, text="tres", start_seconds=0.4, end_seconds=0.7),
    ]

    with pytest.raises(ValueError, match="consecutive IDs"):
        asyncio.run(
            build_beat_timings(
                SourceScript(text="uno tres"),
                [Beat(id=1, block_id=1, action="Una acción.")],
                words,
                NarrationAudio(uri="narration.wav", duration_seconds=1.0),
                timing_bot=FakeTimingBot([3]),  # type: ignore[arg-type]
            )
        )


def test_beat_timing_workflow_rejects_nonpositive_derived_interval() -> None:
    words = [
        NarrationWord(id=1, text="uno", start_seconds=0.0, end_seconds=0.0),
        NarrationWord(id=2, text="dos", start_seconds=0.0, end_seconds=0.2),
    ]

    with pytest.raises(ValueError, match="non-positive interval"):
        asyncio.run(
            build_beat_timings(
                SourceScript(text="uno dos"),
                [
                    Beat(id=1, block_id=1, action="Primera acción."),
                    Beat(id=2, block_id=1, action="Segunda acción."),
                ],
                words,
                NarrationAudio(uri="narration.wav", duration_seconds=1.0),
                timing_bot=FakeTimingBot([1, 2]),  # type: ignore[arg-type]
            )
        )
