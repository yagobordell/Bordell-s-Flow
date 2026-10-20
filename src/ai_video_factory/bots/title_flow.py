"""T1/T2 Claude Haiku orchestration, validation and immutable turn history."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.request
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from ai_video_factory.bots.contracts import ProfessionalB2Block
from ai_video_factory.providers.grid_numbering import (
    alphabetic_sequence_id, format_grid_marker, parse_grid_marker,
)

MODEL = "claude-haiku-5-5"
BEAT_TYPES = set("setup orientation action evidence claim data question revelation escalation consequence contrast analogy causal_link correction reaction pivot verdict transition return".split())
BLOCK_TYPES = set("intro development pivot correction climax close".split())


class TitleContractError(ValueError):
    pass


class ClaudeAPIError(RuntimeError):
    """Ambiguous provider failure. The paid request MUST NOT be repeated blindly."""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path: Path, data: Any) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise TitleContractError(f"Unsafe checkpoint: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    if tmp.exists() or tmp.is_symlink():
        raise TitleContractError(f"Unreconciled checkpoint: {tmp}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise TitleContractError(f"Missing checkpoint: {path}")
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise TitleContractError(f"Malformed checkpoint: {path}")
    return obj


def prompt(name: str) -> str:
    return files("ai_video_factory.bots.prompts").joinpath(name).read_text(encoding="utf-8")


def parse(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("stop_reason") != "end_turn":
        raise TitleContractError(f"Incomplete Claude response: {response.get('stop_reason')}")
    text = "".join(p.get("text", "") for p in response["content"] if p["type"] == "text").strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        text = re.sub(r"^" + fence + r"(?:json)?\s*|\s*" + fence + r"$", "", text)
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise TitleContractError("Invalid Claude JSON") from exc
    if not isinstance(value, dict):
        raise TitleContractError("Claude JSON must be an object")
    return value


def call_claude(
    *, api_key: str, system: str, messages: list[dict[str, Any]],
    transport: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Streaming Anthropic Messages API, retaining signed thinking content intact."""
    body = {
        "model": MODEL, "max_tokens": 32000, "system": system, "messages": messages,
        "output_config": {"effort": "high"}, "cache_control": {"type": "ephemeral"},
        "stream": True,
    }
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "x-api-key": api_key, "anthropic-version": "2023-06-01",
            "content-type": "application/json", "accept": "text/event-stream",
        }, method="POST",
    )
    blocks: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    reason = None
    stopped = False
    try:
        opener = transport or urllib.request.urlopen
        with opener(request, timeout=600) as stream:
            for line in stream:
                line = line.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                kind = event.get("type")
                if kind == "error":
                    raise ClaudeAPIError("Claude SSE returned an error")
                if kind == "message_start":
                    usage.update(event.get("message", {}).get("usage") or {})
                elif kind == "content_block_start":
                    blocks[event["index"]] = event["content_block"]
                elif kind == "content_block_delta":
                    block = blocks[event["index"]]
                    delta = event["delta"]
                    if delta["type"] == "text_delta":
                        block["text"] = block.get("text", "") + delta["text"]
                    elif delta["type"] == "thinking_delta":
                        block["thinking"] = block.get("thinking", "") + delta["thinking"]
                    elif delta["type"] == "signature_delta":
                        block["signature"] = block.get("signature", "") + delta["signature"]
                elif kind == "message_delta":
                    reason = event.get("delta", {}).get("stop_reason", reason)
                    usage.update(event.get("usage") or {})
                elif kind == "message_stop":
                    stopped = True
    except ClaudeAPIError:
        raise
    except (OSError, ValueError, KeyError, UnicodeError) as exc:
        raise ClaudeAPIError(
            "Claude call outcome is uncertain; inspect saved state before retrying"
        ) from exc
    if not stopped:
        raise ClaudeAPIError("Claude SSE ended without message_stop; manual reconciliation")
    return {"model": MODEL, "stop_reason": reason, "usage": usage,
            "content": [blocks[i] for i in sorted(blocks)]}


def image(data: bytes) -> dict[str, Any]:
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/png",
        "data": base64.b64encode(data).decode("ascii"),
    }}


def validate_plan(value: dict[str, Any]) -> None:
    if value.get("schema_version") != "b-planner-v1":
        raise TitleContractError("T1 wrong schema")
    core = value.get("narrative_contract")
    if not isinstance(core, dict) or not all(
        isinstance(core.get(k), str) and core[k].strip()
        for k in ("central_question", "final_answer")
    ):
        raise TitleContractError("T1 narrative contract missing")
    stages = value.get("stages")
    if not isinstance(stages, list) or not stages:
        raise TitleContractError("T1 has no stages")
    total = 0
    for i, stage in enumerate(stages, 1):
        if not isinstance(stage, dict) or stage.get("id") != f"S{i:02d}":
            raise TitleContractError(f"T1 bad stage {i}")
        if stage.get("kind") not in ("hook", "chain", "aside", "closing"):
            raise TitleContractError(f"T1 bad kind {i}")
        n = stage.get("word_budget")
        if type(n) is not int or n <= 0 or not isinstance(stage.get("markers"), list):
            raise TitleContractError(f"T1 bad stage budget/markers {i}")
        total += n
    if total != value.get("word_budget_total"):
        raise TitleContractError("T1 budgets do not sum")


def is_ebook_cta(stage: dict[str, Any]) -> bool:
    return stage["kind"] == "aside" and bool(
        set(stage["markers"]) & {"cta_intermediate", "cta_final"}
    )


def validate_block(
    wrapper: dict[str, Any], number: int, plan: dict[str, Any],
    accepted: list[dict[str, Any]],
) -> dict[str, Any]:
    block = wrapper.get("block")
    if not isinstance(block, dict) or block.get("block_id") != number:
        raise TitleContractError(f"T2 expected block {number}")
    stage = plan["stages"][number - 1]
    if block.get("type") not in BLOCK_TYPES:
        raise TitleContractError("T2 invalid block type")
    if stage["kind"] == "hook" and block["type"] != "intro":
        raise TitleContractError("T2 hook must be intro")
    if stage["kind"] == "closing" and block["type"] != "close":
        raise TitleContractError("T2 closing must be close")
    beats = block.get("beats")
    if not isinstance(beats, list) or not beats:
        raise TitleContractError("T2 block has no beats")
    cta = is_ebook_cta(stage)
    count_ctas = sum(1 for b in accepted if "cta_group_id" in b["beats"][0])
    group_counts: dict[str, int] = {}
    audio_run = 0
    for prior in accepted:
        for beat in prior["beats"]:
            label = parse_grid_marker(beat.get("prompt"))
            if label and label.style == "lettered":
                group_counts[label.sequence_id] = group_counts.get(label.sequence_id, 0) + 1
            audio_run = audio_run + len(beat["text"].strip()) if beat["audio"] else 0
    words = 0
    for index, beat in enumerate(beats, 1):
        if not isinstance(beat, dict):
            raise TitleContractError("T2 beat must be object")
        beat_id = f"{number}{alphabetic_sequence_id(index)}"
        if beat.get("beat_id") != beat_id:
            raise TitleContractError(f"T2 expected beat {beat_id}")
        text = beat.get("text")
        if not isinstance(text, str) or not text.strip():
            raise TitleContractError(f"T2 empty text {beat_id}")
        if len(text) > 100 or len(text.split()) > 40:
            raise TitleContractError(f"T2 beat {beat_id} too long")
        words += len(text.split())
        if beat.get("beat_type") not in BEAT_TYPES:
            raise TitleContractError(f"T2 beat type invalid {beat_id}")
        visual = beat.get("visual_type")
        p, v = beat.get("prompt"), beat.get("video_prompt")
        a, ref = beat.get("audio"), beat.get("reference")
        if type(ref) is not bool or type(a) is not bool:
            raise TitleContractError(f"T2 invalid bools {beat_id}")
        if visual == "avatar":
            valid = p is None and v is None and a and not ref
        elif visual == "avatar_media":
            valid = isinstance(p, str) and bool(p.strip()) and v is None and a
        elif visual == "media_image":
            valid = isinstance(p, str) and bool(p.strip()) and v is None and not a
        elif visual == "media_video":
            valid = isinstance(p, str) and bool(p.strip()) and isinstance(v, str) and bool(v.strip()) and not a
        else:
            valid = False
        if not valid:
            raise TitleContractError(f"T2 visual contract invalid {beat_id}")
        audio_run = audio_run + len(text.strip()) if a else 0
        if audio_run > 120:
            raise TitleContractError(f"T2 presenter sequence >120 chars at {beat_id}")
        if cta:
            sources = beat.get("cta_reference_sources")
            if beat.get("cta_group_id") != count_ctas + 1 or sources not in ([], ["ebook"]):
                raise TitleContractError(f"T2 CTA metadata invalid {beat_id}")
            if ref != (sources == ["ebook"]):
                raise TitleContractError(f"T2 CTA reference mismatch {beat_id}")
        elif "cta_group_id" in beat or "cta_reference_sources" in beat:
            raise TitleContractError(f"T2 non-CTA has CTA metadata {beat_id}")
        marker = parse_grid_marker(p)
        if marker is not None:
            if marker.style != "lettered" or visual != "media_image" and visual != "media_video" or cta:
                raise TitleContractError(f"T2 invalid grid marker {beat_id}")
            expected = group_counts.get(marker.sequence_id, 0) + 1
            if expected != marker.panel_number or expected > 24:
                raise TitleContractError(f"T2 invalid {marker.sequence_id} numbering (max 24)")
            if marker.sequence_id not in group_counts and marker.sequence_id != alphabetic_sequence_id(len(group_counts) + 1):
                raise TitleContractError(f"T2 new grid letter out of order {beat_id}")
            group_counts[marker.sequence_id] = expected
    if words < stage["word_budget"] * 0.9:
        raise TitleContractError(f"T2 block {number} only {words}/{stage['word_budget']} words")
    if block["type"] == "close":
        last = beats[-1]
        if last["visual_type"] != "avatar" or last["reference"] or last["prompt"] is not None:
            raise TitleContractError("T2 final beat must reuse avatar")
    plain = {**block, "beats": [
        {k: v for k, v in beat.items() if k not in ("cta_group_id", "cta_reference_sources")}
        for beat in beats
    ]}
    try:
        ProfessionalB2Block.model_validate(plain)
    except ValueError as exc:
        raise TitleContractError(f"T2 professional schema invalid: {exc}") from exc
    return block


def merged(plan: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    prior: list[dict[str, Any]] = []
    for n, block in enumerate(blocks, 1):
        validate_block({"block": block}, n, plan, prior)
        prior.append(block)
    if len(blocks) != len(plan["stages"]):
        raise TitleContractError("T2 has missing stages")
    return {
        "schema_version": "b-pipeline-merged-v1", "pipeline_stage": "B2",
        "visual_style": "profesional",
        "narrative_core": {
            "central_question": plan["narrative_contract"]["central_question"],
            "final_answer": plan["narrative_contract"]["final_answer"],
        },
        "blocks": blocks,
    }


def image_plan(value: dict[str, Any]) -> dict[str, Any]:
    """Rebase global T2 letters onto the existing block-local image generator."""
    data = json.loads(json.dumps(value, ensure_ascii=False))
    for block in data["blocks"]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for beat in block["beats"]:
            marker = parse_grid_marker(beat.get("prompt"))
            if marker and marker.style == "lettered":
                beat["title_global_group"] = marker.sequence_id
                beat["title_global_position"] = marker.panel_number
                groups.setdefault(marker.sequence_id, []).append(beat)
        reserved = set(groups)
        serial = 1
        for old_id, items in groups.items():
            for offset in range(0, len(items), 20):
                chunk = items[offset:offset + 20]
                if len(chunk) < 2:
                    chunk[0]["prompt"] = parse_grid_marker(chunk[0]["prompt"]).body
                    continue
                if offset:
                    while alphabetic_sequence_id(serial) in reserved:
                        serial += 1
                    new_id = alphabetic_sequence_id(serial)
                    reserved.add(new_id)
                else:
                    new_id = old_id
                for n, beat in enumerate(chunk, 1):
                    body = parse_grid_marker(beat["prompt"]).body
                    beat["prompt"] = format_grid_marker("lettered", new_id, n, body)
    return {
        "schema_version": "b-pipeline-v1", "flow": "titulo",
        "visual_style": "profesional", "narrative_core": data["narrative_core"],
        "blocks": data["blocks"],
    }


def run(
    *, output: Path, avatar: str, title: str, ebook: bytes, thumbnail: bytes,
    api_key: str, stop: str | None = None,
    provider: Callable[..., dict[str, Any]] = call_claude,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    identity = {
        "schema_version": "title-input-v1", "avatar": avatar, "title": title,
        "ebook_sha256": sha(ebook), "thumbnail_sha256": sha(thumbnail),
        "t1_prompt_sha256": sha(prompt("t1_planner.md").encode()),
        "t2_prompt_sha256": sha(prompt("t2_director.md").encode()),
        "model": MODEL, "effort": "high",
    }
    input_path = output / "T1" / "input.json"
    if input_path.is_file():
        if read(input_path) != identity:
            raise TitleContractError("Title inputs/prompts changed; refuse stale checkpoints")
    else:
        write(input_path, identity)
    images = [image(ebook), image(thumbnail)]
    title_text = f"Avatar: {avatar}\nTitle: {title}"
    first = [{"role": "user", "content": [*images, {"type": "text", "text": title_text}]}]
    result_path = output / "T1" / "output.json"
    if result_path.is_file():
        plan = read(result_path)
        validate_plan(plan)
    else:
        # A completed HTTP response may have been checkpointed just before
        # the process stopped. Reconcile it, never submit the same turn twice.
        accepted_plan = None
        for attempt in range(1, 4):
            response_path = output / "T1" / f"response_attempt_{attempt}.json"
            if response_path.is_file():
                response = read(response_path)
            else:
                if attempt > 1 and not (
                    output / "T1" / f"response_attempt_{attempt - 1}.json"
                ).is_file():
                    raise TitleContractError("T1 previous request outcome uncertain")
                response = provider(
                    api_key=api_key, system=prompt("t1_planner.md"), messages=first,
                )
                write(response_path, response)
            try:
                candidate = parse(response)
                validate_plan(candidate)
            except TitleContractError:
                if attempt == 3:
                    raise
                continue
            accepted_plan = candidate
            break
        if accepted_plan is None:
            raise TitleContractError("T1 exhausted validation retries")
        plan = accepted_plan
        write(result_path, plan)
    if stop == "T1":
        return plan, None
    first_text = (
        title_text + "\nPlanning JSON from Bot 1:\n"
        + json.dumps(plan, ensure_ascii=False) + "\nWrite block 1."
    )
    history: list[dict[str, Any]] = [
        {"role": "user", "content": [*images, {"type": "text", "text": first_text}]}
    ]
    blocks: list[dict[str, Any]] = []
    for number in range(1, len(plan["stages"]) + 1):
        if number > 1:
            history.append({"role": "user", "content": f"Write block {number}."})
        root = output / "T2" / "blocks" / f"block_{number:03d}"
        out_file = root / "output.json"
        resp_file = root / "response.json"
        if out_file.is_file() or resp_file.is_file():
            if not (out_file.is_file() and resp_file.is_file()):
                # A raw accepted response may have been saved before the
                # validated output was committed. Reconstruct it deterministically.
                if not resp_file.is_file():
                    raise TitleContractError("Accepted T2 result lacks original response")
                response = read(resp_file)
                result = parse(response)
                validate_block(result, number, plan, blocks)
                write(out_file, result)
            result = read(out_file)
            response = read(resp_file)
            block = validate_block(result, number, plan, blocks)
            if parse(response) != result:
                raise TitleContractError("T2 raw response differs from accepted checkpoint")
        else:
            input_path = root / "input.json"
            identity = {
                "block_id": number, "prior_blocks": len(blocks),
                "plan_sha256": sha(json.dumps(plan, sort_keys=True).encode()),
            }
            if input_path.is_file():
                if read(input_path) != identity:
                    raise TitleContractError("T2 prior turn input differs")
                if not list(root.glob("response_attempt_*.json")):
                    raise TitleContractError(
                        "T2 request may already have been submitted without a response; "
                        "manual reconciliation is required"
                    )
            else:
                write(input_path, identity)
            for attempt in range(1, 4):
                attempt_path = root / f"response_attempt_{attempt}.json"
                if attempt_path.is_file():
                    response = read(attempt_path)
                else:
                    response = provider(
                        api_key=api_key, system=prompt("t2_director.md"), messages=history,
                    )
                    write(attempt_path, response)
                try:
                    result = parse(response)
                    block = validate_block(result, number, plan, blocks)
                except TitleContractError as exc:
                    rejection = root / f"rejection_{attempt}.json"
                    if not rejection.is_file():
                        write(rejection, {"reason": str(exc)})
                    if attempt == 3:
                        raise
                    continue
                write(resp_file, response)
                write(out_file, result)
                break
        blocks.append(block)
        history.append({"role": "assistant", "content": response["content"]})
    value = merged(plan, blocks)
    write(output / "T2" / "merged_output.json", value)
    return plan, value
