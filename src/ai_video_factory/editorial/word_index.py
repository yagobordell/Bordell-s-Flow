"""Offline word timing from verified saved STT: never makes another STT request."""
from __future__ import annotations

import json
from typing import Any

from ai_video_factory.compositor.static_preview import (
    StaticPreviewError,
    _align_stt_tokens,
    _spoken_tokens,
    _tokens,
    _verified_asset,
)


def build_word_index(
    output: Any, plan: dict[str, Any], audio: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    blocks = plan["blocks"]
    recorded = audio["blocks"]
    groups = audio.get("block_groups")
    if groups is None:
        if len(blocks) != len(recorded):
            raise StaticPreviewError("STT editorial source block mismatch")
        sources = [[block] for block in blocks]
    else:
        sources = []
        cursor = 0
        for spec, record in zip(groups, recorded, strict=True):
            ids = spec["source_block_ids"]
            subset = blocks[cursor:cursor + len(ids)]
            if [block["block_id"] for block in subset] != ids or record["block_id"] != ids[0]:
                raise StaticPreviewError("Editorial STT group differs from original block order")
            sources.append(subset)
            cursor += len(ids)
        if cursor != len(blocks) or len(sources) != len(recorded):
            raise StaticPreviewError("Editorial STT groups are incomplete")
    result: dict[str, list[dict[str, Any]]] = {}
    starts = audio["block_start_seconds"]
    for index, (source_group, recording) in enumerate(zip(sources, recorded, strict=True)):
        stt = recording["stt"]
        path = _verified_asset(output, stt["file"], stt["sha256"])
        transcript = json.loads(path.read_text(encoding="utf-8"))
        if (transcript.get("source_audio_sha256") != recording["sha256"]
            or transcript.get("model") != stt["model"]):
            raise StaticPreviewError("Editorial STT identity mismatch")
        spoken = []
        for word in transcript["words"]:
            for token in _tokens(word["word"]):
                spoken.append({
                    "token": token, "start": float(word["start"]),
                    "end": float(word["end"]),
                })
        beats = [beat for block in source_group for beat in block["beats"]]
        expected = [_spoken_tokens(beat["text"]) for beat in beats]
        flat = [word for beat_words in expected for word in beat_words]
        mapping = _align_stt_tokens(
            flat, [word["token"] for word in spoken], block_id=source_group[0]["block_id"],
        )
        cursor = 0
        for beat, words in zip(beats, expected, strict=True):
            if beat["beat_id"] in result:
                raise StaticPreviewError("Duplicate beat in editorial STT index")
            values = []
            for offset, token in enumerate(words):
                match = mapping[cursor + offset]
                item = spoken[match] if match is not None else None
                values.append({
                    "token": token,
                    "start_seconds": starts[index] + item["start"] if item is not None else None,
                    "end_seconds": starts[index] + item["end"] if item is not None else None,
                })
            result[beat["beat_id"]] = values
            cursor += len(words)
    return result
