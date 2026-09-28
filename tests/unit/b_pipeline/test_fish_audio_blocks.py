"""Per-block Fish + AI33 concurrency and deterministic narration assembly."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.bots import fish_audio_workflow as fish_workflow
from ai_video_factory.bots.contracts import B11Output
from ai_video_factory.bots.fish_audio import (
    FishAudioScript,
    FishAudioScriptError,
    validate_fish_script,
)

SCRIPT = "Uno. Dos."
VOICE = "fishaudio_80e34d5e0b2b4577a486f3a77e357261"
B11 = B11Output.model_validate({
    "pipeline_stage": "B1.1",
    "narrative_core": {"central_question": "¿Por qué?", "final_answer": "Dos."},
    "blocks": [
        {
            "block_id": 1,
            "type": "intro",
            "emotional_entry": "calm",
            "emotional_exit": "curious",
            "span": {"first_words": "Uno.", "last_words": "Uno."},
        },
        {
            "block_id": 2,
            "type": "close",
            "emotional_entry": "curious",
            "emotional_exit": "relieved",
            "span": {"first_words": "Dos.", "last_words": "Dos."},
        },
    ],
    "error": None,
})


class ParallelDirector:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.both_started = asyncio.Event()

    async def generate_structured_with_response(self, **kwargs):
        payload = json.loads(kwargs["input_text"])
        self.calls.append(payload)
        if len(self.calls) == 2:
            self.both_started.set()
        await asyncio.wait_for(self.both_started.wait(), timeout=2)
        return (
            FishAudioScript(
                plain_script_for_recording="[warm]" + payload["blocks"][0]["text"]
            ),
            SimpleNamespace(usage=None),
        )


class IndependentSpeech:
    def __init__(self) -> None:
        self.creates: list[dict] = []

    def create(self, **kwargs):
        self.creates.append(kwargs)
        block_id = 1 if "Uno." in kwargs["text"] else 2
        return {"data": {"task_id": f"speech-{block_id}"}}

    def poll(self, task_id, **_kwargs):
        return {
            "status": "done",
            "credit_cost": 13,
            "metadata": {"audio_url": f"https://example.test/{task_id}.mp3"},
        }

    def download_audio(self, url, directory):
        directory.mkdir(parents=True, exist_ok=True)
        data = b"ID3" + url.encode("utf-8")
        path = directory / "audio.mp3"
        path.write_bytes(data)
        return {
            "file": path.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "format": "mp3",
            "bytes": len(data),
        }


def test_parallel_directors_and_paid_tasks_join_in_source_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    director = ParallelDirector()
    speech = IndependentSpeech()
    captured: list[list[str]] = []

    def fake_join(inputs: list[Path], destination: Path) -> dict[str, object]:
        captured.append([path.parent.name for path in inputs])
        destination.write_bytes(b"RIFF" + b"\x00" * 40)
        payload = destination.read_bytes()
        return {
            "file": destination.name,
            "format": "wav",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "duration_seconds": 3.0,
        }

    monkeypatch.setattr(fish_workflow, "join_audio_blocks", fake_join)
    result = asyncio.run(asyncio.wait_for(
        fish_workflow.generate_fish_audio(
            SCRIPT,
            tmp_path,
            b11=B11,
            director_model="gpt-6-luna",
            provider=director,
            ai33_api_key=None,
            speech_client=speech,
            max_parallel_calls=2,
        ),
        timeout=3,
    ))
    assert director.both_started.is_set()
    assert {payload["current_block_id"] for payload in director.calls} == {1, 2}
    for payload in director.calls:
        assert len(payload["blocks"]) == 1
        assert payload["current_block_id"] == payload["blocks"][0]["block_id"]
        assert payload["narrative_core"] == B11.narrative_core.model_dump()
    assert len(speech.creates) == 2
    assert all(item["voice_id"] == VOICE for item in speech.creates)
    assert [item["block_id"] for item in result["blocks"]] == [1, 2]
    assert [item["task_id"] for item in result["blocks"]] == ["speech-1", "speech-2"]
    assert captured == [["block_1", "block_2"]]
    assert result["pause_between_blocks_seconds"] == 0.5
    assert result["credit_cost"] == 26
    assert (tmp_path / result["file"]).is_file()


@pytest.mark.parametrize(
    ("source", "directed"),
    [
        ("Hola, ¿qué tal?", "[warm]Hola ¿qué tal!"),
        ("A.\r\nB.", "[warm]A. \nB!"),
        ("[existing] Hola.", "[warm][existing] Hola!"),
    ],
)
def test_director_tolerates_layout_and_punctuation(
    source: str, directed: str,
) -> None:
    assert validate_fish_script(source, directed) == ("[warm]",)


def test_composite_natural_language_tag_is_accepted() -> None:
    assert validate_fish_script("Hola.", "[warm, measured]Hola.") == (
        "[warm, measured]",
    )


@pytest.mark.parametrize(
    "directed",
    ["[warm]Casa.", "[warm]Hoya.", "[warm]Hola. Texto nuevo"],
)
def test_director_rejects_lexical_changes(directed: str) -> None:
    with pytest.raises(FishAudioScriptError):
        validate_fish_script("Hola.", directed)
