import asyncio

from ai_video_factory.domain import Beat, NarrativeBlock, Scene, SourceScript
from ai_video_factory.workflows.narrative_planning import plan_narrative


class FakeBlockBot:
    async def run(self, source: SourceScript) -> list[NarrativeBlock]:
        assert source.text == "Guion de prueba"
        return [
            NarrativeBlock(id=1, text="Bloque uno"),
            NarrativeBlock(id=2, text="Bloque dos"),
        ]


class ParallelOnlyBeatBot:
    """The first call can finish only after the second call has started."""

    def __init__(self) -> None:
        self.started = 0
        self.all_started = asyncio.Event()

    async def run(self, block: NarrativeBlock) -> list[str]:
        self.started += 1
        if self.started == 2:
            self.all_started.set()

        await asyncio.wait_for(self.all_started.wait(), timeout=0.5)
        return [f"Acción {block.id}.1", f"Acción {block.id}.2"]


class RecordingSceneBot:
    def __init__(self) -> None:
        self.received: list[Beat] | None = None

    async def run(self, beats: list[Beat]) -> list[Scene]:
        self.received = beats
        return [
            Scene(id=1, beat_ids=[1, 2]),
            Scene(id=2, beat_ids=[3, 4]),
        ]


def test_plan_narrative_extracts_beats_in_parallel_and_assigns_stable_ids() -> None:
    beat_bot = ParallelOnlyBeatBot()
    scene_bot = RecordingSceneBot()

    blocks, beats, scenes = asyncio.run(
        plan_narrative(
            SourceScript(text="Guion de prueba"),
            block_bot=FakeBlockBot(),  # type: ignore[arg-type]
            beat_bot=beat_bot,  # type: ignore[arg-type]
            scene_bot=scene_bot,  # type: ignore[arg-type]
        )
    )

    assert beat_bot.started == 2
    assert [block.id for block in blocks] == [1, 2]
    assert [beat.model_dump() for beat in beats] == [
        {"id": 1, "block_id": 1, "action": "Acción 1.1"},
        {"id": 2, "block_id": 1, "action": "Acción 1.2"},
        {"id": 3, "block_id": 2, "action": "Acción 2.1"},
        {"id": 4, "block_id": 2, "action": "Acción 2.2"},
    ]
    assert scene_bot.received == beats
    assert [scene.beat_ids for scene in scenes] == [[1, 2], [3, 4]]
