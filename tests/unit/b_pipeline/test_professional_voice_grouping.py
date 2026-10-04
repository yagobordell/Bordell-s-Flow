"""Progressive professional voice grouping and original-B2 audio alignment."""

import asyncio
import copy
import hashlib
import io
import json
import wave
from types import SimpleNamespace

import pytest

from ai_video_factory.bots import professional_voice as pv
from ai_video_factory.compositor.static_preview import (
    StaticPreviewError,
    build_stt_beat_timeline,
)


def _blocks(sizes: list[int]) -> list[dict]:
    blocks = []
    for identifier, size in enumerate(sizes, 1):
        beats = [
            {"beat_id": f"{identifier}{chr(65 + j)}", "text": f"word{identifier}{j} ",
             "beat_type": "setup", "visual_type": "media_image", "prompt": "still",
             "video_prompt": None, "reference": False, "audio": False}
            for j in range(size)
        ]
        blocks.append({
            "block_id": identifier, "type": "development",
            "emotional_entry": "curious", "emotional_exit": "focused",
            "span": {"first_words": beats[0]["text"], "last_words": beats[-1]["text"]},
            "beats": beats,
        })
    return blocks


@pytest.mark.parametrize(("sizes", "expected"), [
    ([6, 1, 3, 8], [[1, 2, 3], [4]]),
    ([5, 1, 5], [[1, 2], [3]]),  # equal neighbours: take preceding
    ([1, 1, 2, 4, 9], [[1, 2, 3, 4], [5]]),
    ([1, 2, 3, 4], [[1, 2, 3, 4]]),
    ([5, 5], [[1], [2]]),
    ([1], [[1]]),
])
def test_progressive_shortest_neighbour_groups(sizes, expected):
    source = _blocks(sizes)
    before = copy.deepcopy(source)
    groups = pv._progressive_voice_groups(source)
    assert groups == expected
    assembled = pv._assemble_voice_blocks(source, groups)
    assert source == before  # B2 is never rewritten
    assert [b["beat_id"] for block in assembled for b in block["beats"]] == [
        b["beat_id"] for block in source for b in block["beats"]
    ]
    assert [a["block_id"] for a in assembled] == [g[0] for g in expected]


def test_group_preserves_original_narrative_boundaries():
    source = _blocks([6, 1, 3, 8])
    source[1]["type"] = "pivot"
    source[2]["emotional_exit"] = "alarmed"
    voice = pv._assemble_voice_blocks(source, pv._progressive_voice_groups(source))
    assert voice[0]["source_block_ids"] == [1, 2, 3]
    assert [b["type"] for b in voice[0]["source_blocks"]] == [
        "development", "pivot", "development",
    ]
    assert voice[0]["emotional_exit"] == "alarmed"
    assert voice[1] is source[3]


def test_pre_feature_checkpoints_keep_original_mapping(tmp_path):
    (tmp_path / "V1").mkdir()
    source = _blocks([6, 1, 3, 8])
    assert pv._voice_blocks_for_plan(source, tmp_path) == source
    assert not (tmp_path / "V1" / "voice_block_plan.json").exists()


def test_corrected_b2_recomputes_persisted_group_plan(tmp_path):
    source = _blocks([6, 1, 3, 8])
    first = pv._voice_blocks_for_plan(source, tmp_path)
    assert [b.get("source_block_ids", [b["block_id"]]) for b in first] == [
        [1, 2, 3], [4],
    ]
    assert pv._voice_blocks_for_audio(source, tmp_path) == first
    changed = copy.deepcopy(source)
    changed[1]["beats"].append({**changed[1]["beats"][0], "beat_id": "2Z"})
    second = pv._voice_blocks_for_plan(changed, tmp_path)
    assert [b.get("source_block_ids", [b["block_id"]]) for b in second] == [
        [1], [2, 3], [4],
    ]
    assert pv._voice_blocks_for_audio(changed, tmp_path) == second


class GroupAwareProvider:
    def __init__(self):
        self.calls = []
        self.inputs = []

    async def generate_structured_with_response(self, *, output_type, input_text, **kwargs):
        payload = json.loads(input_text)["payload"]
        self.calls.append(output_type.__name__)
        self.inputs.append(payload)
        if output_type is pv.V1:
            value = pv.V1(
                pipeline_stage="V1", curve="Complete narrative",
                block_rhythms=[
                    {"block_id": block["block_id"], "phase": "Advances the narrative",
                     "viewer_trajectory": "From opening to completion",
                     "tempo": "measured", "pressure": "moderate",
                     "contrast": "Relative to its neighbours"}
                    for block in payload["blocks"]
                ], baseline_reconsiderations=[],
            )
        elif output_type is pv.V2:
            block = payload["current_block"]
            beats = block["beats"]
            value = pv.V2(
                pipeline_stage="V2", block_id=block["block_id"],
                beat_intents=[
                    {"beat_id": beat["beat_id"], "function": "Advances this run",
                     "understanding_delta": "Gains a new piece of knowledge",
                     "viewer_trajectory": "Increasingly attentive",
                     "tempo_modulation": "at", "pressure_modulation": "at"}
                    for beat in beats
                ], landing_beat_id=beats[-1]["beat_id"], baseline_misfit=[],
            )
        elif output_type is pv.V3:
            block = payload["current_block"]
            value = pv.V3(
                pipeline_stage="V3", block_id=block["block_id"],
                block_performance={
                    "register": "Clear documentary narrator",
                    "pace_character": "Measured progression",
                    "intensity_arc": "Subtle progression through the block",
                },
                beat_deliveries=[
                    {"beat_id": beat["beat_id"],
                     "delivery": [
                         {"speak": beat["text"], "delivery_quality": "Natural delivery",
                          "ending_behavior": "carry", "stress": [], "vocal_intents": []}
                     ]}
                    for beat in block["beats"]
                ],
            )
        elif output_type is pv.V4:
            direction = pv.V3.model_validate(payload["v3"])
            value = pv.V4(
                pipeline_stage="V4", block_id=direction.block_id,
                audio_profile="Clear documentary narrator",
                directors_notes=["Measured, subtly increasing focus."],
                spoken_directions=[
                    {"beat_id": beat.beat_id, "source_item_index": i,
                     "locator": item.speak.strip(),
                     "instruction": "Natural delivery; keep cadence open",
                     "ending_behavior": item.ending_behavior,
                     "stress": item.stress}
                    for beat in direction.beat_deliveries
                    for i, item in enumerate(beat.delivery)
                    if isinstance(item, pv.Speak)
                ],
                beat_tags=[
                    {"beat_id": beat.beat_id, "text": "", "inserts": []}
                    for beat in direction.beat_deliveries
                ],
            )
        else:
            raise AssertionError(output_type)
        return value, SimpleNamespace(id=str(len(self.calls)), model="test", usage=None)


def test_full_v1_v2_v3_receive_same_premerged_units_and_resume(tmp_path):
    source = _blocks([6, 1, 3, 8])
    provider = GroupAwareProvider()
    kwargs = dict(
        core={"central_question": "Why?", "final_answer": "Because"},
        blocks=source, output=tmp_path, provider=provider, model="test",
    )
    v1, v2, v3, v4 = asyncio.run(pv.plan_professional_voice(**kwargs))
    assert [r.block_id for r in v1.block_rhythms] == [1, 4]
    assert [r.block_id for r in v2] == [1, 4]
    assert [r.block_id for r in v3] == [1, 4]
    assert [r.block_id for r in v4] == [1, 4]
    assert len(v2[0].beat_intents) == 10
    assert len(v3[0].beat_deliveries) == 10
    assert provider.inputs[0]["blocks"][0]["source_block_ids"] == [1, 2, 3]
    assert sorted(provider.calls) == ["V1", "V2", "V2", "V3", "V3", "V4", "V4"]
    assert (tmp_path / "V1" / "voice_block_plan.json").is_file()
    asyncio.run(pv.plan_professional_voice(**kwargs))
    assert len(provider.calls) == 7  # all cached; no repeat paid V calls


def _wav(seconds: float) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav_file:
        wav_file.setframerate(48000)
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.writeframes(b"\0" * int(48000 * seconds) * 2)
    return stream.getvalue()


def test_grouped_stt_keeps_original_visual_ids_and_exact_pcm_offsets(tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    wav = _wav(3.5)
    (audio_dir / "narration.wav").write_bytes(wav)
    groups = [
        {"block_id": 1, "source_block_ids": [1, 2], "beat_ids": ["1A", "2A"]},
        {"block_id": 3, "source_block_ids": [3], "beat_ids": ["3A"]},
    ]
    block_records = []
    for bid, words in [
        (1, [{"word": "uno", "start": 0.2, "end": 0.4},
             {"word": "dos", "start": 0.8, "end": 1.0}]),
        (3, [{"word": "tres", "start": 0.1, "end": 0.4}]),
    ]:
        path = audio_dir / f"stt_{bid}.json"
        transcript = {"model": "whisper-1", "source_audio_sha256": f"wav{bid}",
                      "words": words}
        data = json.dumps(transcript).encode()
        path.write_bytes(data)
        block_records.append({
            "block_id": bid, "source_block_ids": [1, 2] if bid == 1 else [3],
            "sha256": f"wav{bid}", "stt": {
                "file": f"audio/stt_{bid}.json",
                "sha256": hashlib.sha256(data).hexdigest(), "model": "whisper-1",
            },
        })
    plan = {"blocks": [
        {"block_id": identifier, "beats": [
            {"beat_id": f"{identifier}A", "text": word, "visual_type": "media_image",
             "description": "still"}
        ]}
        for identifier, word in [(1, "uno"), (2, "dos"), (3, "tres")]
    ]}
    audio = {
        "blocks": block_records, "block_groups": groups,
        "file": "audio/narration.wav", "sha256": hashlib.sha256(wav).hexdigest(),
        "stt_complete": True, "duration_seconds": 3.5,
        "block_start_seconds": [0.0, 2.5], "block_durations_seconds": [2.0, 1.0],
    }
    result = build_stt_beat_timeline(tmp_path, plan, audio)
    beats = result["beats"]
    assert [(beat["block_id"], beat["beat_id"]) for beat in beats] == [
        (1, "1A"), (2, "2A"), (3, "3A"),
    ]
    assert [beat["start_seconds"] for beat in beats] == [0.0, 0.8, 2.5]
    corrupted = copy.deepcopy(audio)
    corrupted["block_groups"][0]["beat_ids"] = ["1A", "3A"]
    with pytest.raises(StaticPreviewError, match="differ"):
        build_stt_beat_timeline(tmp_path, plan, corrupted)


def test_gemini_generates_one_paid_wav_per_group_and_reuses_it(tmp_path, monkeypatch):
    source = _blocks([6, 1, 3, 8])
    director = GroupAwareProvider()

    class Speech:
        def __init__(self):
            self.calls = []

        async def generate_directed_speech(self, *, turns, **_kwargs):
            self.calls.append([turn["text"] for turn in turns])
            return SimpleNamespace(content=_wav(0.1))

    async def transcribe(item, *, output, run_dir, stt_client):
        folder = run_dir / "blocks" / f"block_{item['block_id']}"
        path = folder / "stt.json"
        data = json.dumps({
            "model": stt_client.model, "source_audio_sha256": item["sha256"],
            "words": [{"word": "word", "start": 0.0, "end": 0.05}],
        }).encode()
        path.write_bytes(data)
        return {
            **item, "stt": {"file": path.relative_to(output).as_posix(),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "model": stt_client.model, "word_count": 1},
        }

    def join(paths, destination):
        assert len(paths) == 2
        assert all(path.is_file() for path in paths)
        wav = _wav(0.7)
        destination.write_bytes(wav)
        return {
            "sha256": hashlib.sha256(wav).hexdigest(), "bytes": len(wav),
            "duration_seconds": 0.7, "block_start_seconds": [0.0, 0.6],
            "block_end_seconds": [0.1, 0.7],
            "block_durations_seconds": [0.1, 0.1],
        }

    monkeypatch.setattr(pv, "_transcribe_block", transcribe)
    monkeypatch.setattr(pv, "join_audio_blocks", join)
    speech = Speech()
    kwargs = dict(
        core={"central_question": "Why?", "final_answer": "Because"},
        blocks=source, output=tmp_path, provider=director, model="test",
        gemini_api_key="test", voice_id="Algenib", source_sha256="a" * 64,
        speech_provider=speech, stt_client=SimpleNamespace(model="whisper-1"),
    )
    artifact = asyncio.run(pv.generate_professional_audio(**kwargs))
    assert artifact["status"] == "completed"
    assert artifact["block_count"] == 2
    assert [record["source_block_ids"] for record in artifact["blocks"]] == [
        [1, 2, 3], [4],
    ]
    assert [group["beat_ids"] for group in artifact["block_groups"]] == [
        [beat["beat_id"] for block in source[:3] for beat in block["beats"]],
        [beat["beat_id"] for beat in source[3]["beats"]],
    ]
    assert [len(call) for call in speech.calls] == [1, 1]
    assert sorted(len(call[0].split()) for call in speech.calls) == [8, 10]
    assert len(director.calls) == 7
    second = asyncio.run(pv.generate_professional_audio(**kwargs))
    assert second == artifact
    assert len(speech.calls) == 2
    assert len(director.calls) == 7
