"""Deterministic checks for the proposed whole-script Professional agent plan.

This module is deliberately separate from the existing B pipeline. It does not
change the Casero path or send paid inference requests. Semantic creative reviews
(screen content, actor ownership, motion feasibility) still require the model.
"""
from __future__ import annotations

import re
from hashlib import sha256
from typing import Any

from ai_video_factory.agents.professional.preflight import (
    is_explicit_ebook_cta_anchor,
    requires_ebook_cover,
)
from ai_video_factory.bots.contracts import BeatType, BlockMeta
from ai_video_factory.bots.workflow import materialize_blocks

BEAT_MAX_CHARACTERS = 100
BEAT_MAX_WORDS = 40
SPOKEN_RUN_MAX_CHARACTERS = 120
IMAGE_SPLIT_THRESHOLD = 70
IMAGE_MAX_PROMPTS = 4
AVATAR_MAX_PERCENT = 45
VISUAL_TYPES = frozenset(("avatar", "avatar_media", "media_image", "media_video"))
BEAT_TYPES = frozenset(BeatType.__args__)
NUMBERED_PROMPT = re.compile(r"^\s*(?:[1-9]|1[0-8])\.\s+", re.DOTALL)


def _issue(rule_id: str, message: str, *, severity: str = "error",
           block_id: int | None = None, beat_ids: list[str] | None = None,
           details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "message": message,
        "block_id": block_id,
        "beat_ids": beat_ids or [],
        "details": details or {},
    }


def _status(issues: list[dict[str, Any]]) -> str:
    if any(x["severity"] == "error" for x in issues):
        return "blocked"
    if issues:
        return "passed_with_warnings"
    return "passed"


def _suffix(number: int) -> str:
    letters = ""
    while number:
        number, rem = divmod(number - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _occurrences(text: str, target: str) -> int:
    """Count literal, possibly overlapping occurrences, matching the declared anchor."""
    return sum(
        text.startswith(target, i)
        for i in range(len(text) - len(target) + 1)
    )


def validate_groups(script: str, groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate legacy B1.1-style boundary anchors and materialize exact group text."""
    issues: list[dict[str, Any]] = []
    if not isinstance(script, str) or not script.strip():
        issues.append(_issue("SOURCE_MISSING", "Authoritative script must be nonempty."))
    if not isinstance(groups, list) or not groups:
        issues.append(_issue("GROUPS_MISSING", "At least one group is required."))
    if issues:
        return {"status": "blocked", "issues": issues, "groups": []}

    try:
        metadata = [BlockMeta.model_validate(group) for group in groups]
        materialized = materialize_blocks(script, metadata)
    except (ValueError, TypeError, KeyError) as exc:
        issues.append(_issue("GROUP_BOUNDARIES", str(exc)))
        return {"status": "blocked", "issues": issues, "groups": []}

    return {
        "status": "passed",
        "issues": [],
        "source_sha256": sha256(script.encode("utf-8")).hexdigest(),
        "groups": [
            {**meta.model_dump(), "text": block.text}
            for meta, block in zip(metadata, materialized, strict=True)
        ],
    }


def _image_prompts(beat: dict[str, Any]) -> list[str] | None:
    value = beat.get("image_prompts")
    if not isinstance(value, list) or not all(
        isinstance(prompt, str) and prompt.strip() for prompt in value
    ):
        return None
    return value


def _editor_note_issues(
    beat: dict[str, Any], block_id: int, beat_id: str,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    notes = beat.get("editor_notes", [])
    if not isinstance(notes, list):
        return [_issue("EDITOR_NOTES_SCHEMA", "editor_notes must be a list.",
                       block_id=block_id, beat_ids=[beat_id])]
    text = beat.get("text", "")
    for index, note in enumerate(notes):
        if not isinstance(note, dict):
            issues.append(_issue("EDITOR_NOTE_SCHEMA", f"Note {index + 1} is not an object.",
                                 block_id=block_id, beat_ids=[beat_id]))
            continue
        anchor = note.get("trigger")
        if anchor is None:
            # Unanchored notes describe creative intent, not a timed overlay.
            continue
        if not isinstance(anchor, dict):
            issues.append(_issue("EDITOR_TRIGGER", "Trigger must be an object.",
                                 block_id=block_id, beat_ids=[beat_id]))
            continue
        phrase, occurrence = anchor.get("text"), anchor.get("occurrence")
        if (
            not isinstance(phrase, str) or not phrase
            or type(occurrence) is not int or occurrence < 1
            or not isinstance(text, str)
            or _occurrences(text, phrase) < occurrence
        ):
            issues.append(_issue(
                "EDITOR_TRIGGER",
                f"Note {index + 1} references a missing word/phrase occurrence.",
                block_id=block_id, beat_ids=[beat_id],
                details={"trigger": anchor},
            ))
    return issues


def _validate_visual(
    beat: dict[str, Any], *, block_id: int, protected: bool,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    beat_id = beat["beat_id"]
    visual = beat.get("visual_type")
    audio, reference = beat.get("audio"), beat.get("reference")
    prompt, motion = beat.get("prompt"), beat.get("video_prompt")
    text = beat["text"]
    if visual not in VISUAL_TYPES:
        return [_issue("VISUAL_TYPE", f"Unsupported visual_type: {visual!r}.",
                       block_id=block_id, beat_ids=[beat_id])]
    if type(audio) is not bool or type(reference) is not bool:
        issues.append(_issue("VISUAL_FLAGS", "audio and reference must be booleans.",
                             block_id=block_id, beat_ids=[beat_id]))
        return issues

    image_prompts = beat.get("image_prompts")
    if visual == "media_image":
        validated = _image_prompts(beat)
        if validated is None or not 1 <= len(validated) <= IMAGE_MAX_PROMPTS:
            issues.append(_issue(
                "IMAGE_PROMPT_COUNT", "media_image requires 1–4 nonempty image_prompts.",
                block_id=block_id, beat_ids=[beat_id],
            ))
        else:
            if len(text.strip()) >= IMAGE_SPLIT_THRESHOLD and not protected and len(validated) < 2:
                issues.append(_issue(
                    "LONG_IMAGE_SPLIT",
                    f"media_image beats with >= {IMAGE_SPLIT_THRESHOLD} source characters "
                    "require at least two image prompts.",
                    block_id=block_id, beat_ids=[beat_id],
                    details={"characters": len(text.strip()), "image_prompts": len(validated)},
                ))
            if protected and beat.get("generation_mode") == "grid":
                issues.append(_issue(
                    "CTA_GRID_PROTECTED",
                    "Protected CTA assets use one independent authentic "
                    "reference image, never a continuity grid.",
                    block_id=block_id, beat_ids=[beat_id],
                ))
            if protected and len(validated) != 1:
                issues.append(_issue(
                    "CTA_IMAGE_FROZEN", "CTA image beats keep one original image prompt.",
                    block_id=block_id, beat_ids=[beat_id],
                ))
            stripped = [NUMBERED_PROMPT.sub("", s).strip().casefold() for s in validated]
            if len(set(stripped)) != len(stripped):
                issues.append(_issue(
                    "DUPLICATED_IMAGE_PROMPT", "A beat contains duplicate image prompts.",
                    block_id=block_id, beat_ids=[beat_id],
                ))
            mode = beat.get("generation_mode", "independent")
            if mode not in {"grid", "independent"}:
                issues.append(_issue("IMAGE_GENERATION_MODE", "Invalid image generation mode.",
                                     block_id=block_id, beat_ids=[beat_id]))
            # A one-image beat CAN join a multi-beat continuity grid.
            # Whole-sequence validity is evaluated after reading its peers.
        if prompt is not None or motion is not None or audio:
            issues.append(_issue(
                "IMAGE_CARRIER", "media_image uses image_prompts, prompt=null, "
                "video_prompt=null, audio=false.",
                block_id=block_id, beat_ids=[beat_id],
            ))
    else:
        if image_prompts not in (None, []):
            issues.append(_issue(
                "IMAGE_PROMPT_CARRIER", "Only media_image may have image_prompts.",
                block_id=block_id, beat_ids=[beat_id],
            ))
        if visual == "avatar":
            if prompt is not None or motion is not None or reference or not audio:
                issues.append(_issue(
                    "BASE_AVATAR", "avatar must reuse its original image and speak.",
                    block_id=block_id, beat_ids=[beat_id],
                ))
        elif visual == "avatar_media":
            if not isinstance(prompt, str) or not prompt.strip() or motion is not None or not audio:
                issues.append(_issue(
                    "AVATAR_MEDIA", "avatar_media requires one still-image prompt, "
                    "no motion and audio=true.",
                    block_id=block_id, beat_ids=[beat_id],
                ))
        elif visual == "media_video":
            if (
                not isinstance(prompt, str) or not prompt.strip()
                or not isinstance(motion, str) or not motion.strip() or audio
            ):
                issues.append(_issue(
                    "VIDEO_CARRIER", "media_video requires a start-frame prompt and "
                    "video_prompt, audio=false.",
                    block_id=block_id, beat_ids=[beat_id],
                ))
    if protected:
        sources = beat.get("cta_reference_sources")
        if (
            not isinstance(sources, list)
            or any(x not in {"ebook", "presenter"} for x in sources)
            or len(sources) != len(set(sources))
            or reference != bool(sources)
            or (visual == "avatar" and sources != [])
            or (visual == "avatar_media" and sources != ["ebook"])
            or (visual in {"media_image", "media_video"} and "ebook" not in sources)
        ):
            issues.append(_issue(
                "CTA_REFERENCES", "CTA reference sources must satisfy Professional CTA rules.",
                block_id=block_id, beat_ids=[beat_id],
            ))
    elif "cta_reference_sources" in beat:
        issues.append(_issue(
            "CTA_METADATA", "Only CTA-owned beats may use cta_reference_sources.",
            block_id=block_id, beat_ids=[beat_id],
        ))
    issues.extend(_editor_note_issues(beat, block_id, beat_id))
    return issues


def _rhythm_map(records: list[dict[str, Any]], script: str) -> dict[str, Any]:
    total = len(script)
    changes: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for record in records:
        count = record["image_count"]
        if previous is not None:
            # Two consecutive base-avatar beats share the same picture.
            same_base_avatar = (
                previous["visual_type"] == record["visual_type"] == "avatar"
            )
            if not same_base_avatar:
                changes.append({
                    "kind": "between_beats",
                    "beat_id": record["beat_id"],
                    "source_offset": record["source_start"],
                    "source_fraction": record["source_start"] / total,
                })
        if count > 1:
            for index in range(1, count):
                changes.append({
                    "kind": "within_beat",
                    "beat_id": record["beat_id"],
                    "subshot_index": index + 1,
                    "source_fraction": (
                        record["source_start"]
                        + (record["source_end"] - record["source_start"]) * index / count
                    ) / total,
                    "timing": "estimate_only_until_stt",
                })
        previous = record
    return {
        "basis": "script_characters_not_seconds",
        "beats": records,
        "estimated_visual_changes": len(changes),
        "between_beat_changes": sum(x["kind"] == "between_beats" for x in changes),
        "within_beat_changes": sum(x["kind"] == "within_beat" for x in changes),
        "change_events": changes,
        "deferred_checks": [
            "stt_word_alignment", "min_two_frames_per_media_image_subshot",
            "actual_render_durations",
        ],
    }


def validate_plan(
    script: str,
    groups: list[dict[str, Any]],
    plan: dict[str, Any],
    *,
    protected_cta_beats: dict[str, dict[str, Any]] | None = None,
    ebook_available: bool = True,
    expected_narrative_core: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check a whole-video candidate. No paid work or changes to existing pipeline.

    Input plan: visual_style=profesional, narrative_core, blocks with beat.text,
    beat_type, visual_type, audio, reference, prompt, video_prompt and, for
    media_image, image_prompts (1–4) and optional generation_mode.
    """
    result = validate_groups(script, groups)
    if result["status"] == "blocked":
        return {"status": "blocked", "issues": result["issues"], "rhythm_map": None}
    issues: list[dict[str, Any]] = []
    if not isinstance(plan, dict) or plan.get("visual_style") != "profesional":
        return {
            "status": "blocked",
            "issues": [_issue("PROFESSIONAL_ONLY", "Only professional plans are supported.")],
            "rhythm_map": None,
        }
    blocks = plan.get("blocks")
    materialized = result["groups"]
    if not isinstance(blocks, list) or len(blocks) != len(materialized):
        return {"status": "blocked", "issues": [
            _issue("PLAN_BLOCKS", "Plan must contain one block per materialized group.")
        ], "rhythm_map": None}
    if not isinstance(plan.get("narrative_core"), dict) or set(plan["narrative_core"]) != {
        "central_question", "final_answer",
    }:
        issues.append(_issue("NARRATIVE_CORE", "Plan must contain the B1.1 narrative core."))
    elif (expected_narrative_core is not None
          and plan["narrative_core"] != expected_narrative_core):
        issues.append(_issue(
            "NARRATIVE_CORE_CHANGED",
            "The complete-video plan changed the approved B1.1 narrative_core. "
            "Restore the exact approved central_question/final_answer.",
        ))

    records: list[dict[str, Any]] = []
    all_ids: set[str] = set()
    spoken_runs: list[dict[str, Any]] = []
    current_run: list[str] = []
    spoken_chars = 0
    avatar_count = 0
    avatar_media_count = 0
    cursor = 0

    def finish_run() -> None:
        nonlocal spoken_chars, current_run
        if current_run:
            spoken_runs.append({"beat_ids": list(current_run), "characters": spoken_chars})
            if spoken_chars > SPOKEN_RUN_MAX_CHARACTERS:
                issues.append(_issue(
                    "SPEAKING_RUN",
                    f"Consecutive audio=true run has {spoken_chars} characters "
                    f"(max {SPOKEN_RUN_MAX_CHARACTERS}).",
                    beat_ids=list(current_run),
                    details={"characters": spoken_chars},
                ))
        spoken_chars = 0
        current_run = []

    protected_cta_beats = protected_cta_beats or {}
    authentic_cta_required = requires_ebook_cover(script)
    source_has_ebook_cta_cue = any(
        is_explicit_ebook_cta_anchor(group["text"]) for group in materialized
    )
    cta_covered_anchors: list[str] = []
    for group, block in zip(materialized, blocks, strict=True):
        block_id = group["block_id"]
        if not isinstance(block, dict) or block.get("block_id") != block_id:
            issues.append(_issue("BLOCK_ID", "Wrong block ID/order.", block_id=block_id))
            continue
        for key in ("type", "emotional_entry", "emotional_exit", "span"):
            if key in block and block[key] != group[key]:
                issues.append(_issue("BLOCK_METADATA", f"Modified B1.1 metadata: {key}.",
                                     block_id=block_id))
        beats = block.get("beats")
        if not isinstance(beats, list) or not beats:
            issues.append(_issue("EMPTY_BLOCK", "Block has no beats.", block_id=block_id))
            continue
        reconstructed = ""
        grid_assets: list[str] = []

        def finish_grid_run() -> None:
            nonlocal grid_assets
            if grid_assets and (len(grid_assets) < 2 or len(grid_assets) > 18):
                issues.append(_issue(
                    "GRID_SEQUENCE_SIZE",
                    "A continuous numbered image run must comprise 2–18 "
                    "assets, possibly across several neighboring beats. "
                    "Choose actual related stills and a coherent grid boundary.",
                    block_id=block_id,
                    beat_ids=list(dict.fromkeys(grid_assets)),
                    details={"image_count": len(grid_assets)},
                ))
            grid_assets = []

        for index, beat in enumerate(beats, start=1):
            if not isinstance(beat, dict):
                issues.append(_issue("BEAT_SCHEMA", "Beat must be an object.", block_id=block_id))
                continue
            beat_id = beat.get("beat_id")
            expected_id = f"{block_id}{_suffix(index)}"
            if beat_id != expected_id or beat_id in all_ids:
                issues.append(_issue(
                    "BEAT_ID", f"Expected unique beat_id {expected_id}, got {beat_id!r}.",
                    block_id=block_id,
                ))
            if not isinstance(beat_id, str):
                beat_id = expected_id
            all_ids.add(beat_id)
            text = beat.get("text")
            if not isinstance(text, str) or not text.strip():
                issues.append(_issue("BEAT_TEXT", "Beat text must be nonempty.",
                                     block_id=block_id, beat_ids=[beat_id]))
                continue
            if beat.get("beat_type") not in BEAT_TYPES:
                issues.append(_issue("BEAT_TYPE", "Invalid narrative beat_type.",
                                     block_id=block_id, beat_ids=[beat_id]))
            if len(text.split()) > BEAT_MAX_WORDS or len(text.strip()) > BEAT_MAX_CHARACTERS:
                issues.append(_issue(
                    "BEAT_LENGTH", "Beat exceeds 40 words or 100 characters.",
                    block_id=block_id, beat_ids=[beat_id],
                    details={"characters": len(text.strip()), "words": len(text.split())},
                ))
            reconstructed += text
            protected = beat.get("cta_group_id") is not None
            if protected and not source_has_ebook_cta_cue:
                issues.append(_issue(
                    "CTA_UNGROUNDED",
                    "The original script contains no ebook/QR cue. "
                    "Do not create an ebook CTA from unrelated narration.",
                    block_id=block_id, beat_ids=[beat_id],
                ))
            if authentic_cta_required and is_explicit_ebook_cta_anchor(text):
                if not protected:
                    issues.append(_issue(
                        "CTA_SPOKEN_ASSET_UNPROTECTED",
                        "The source explicitly promotes an ebook/QR. "
                        "The spoken offer beat needs the authentic ebook CTA "
                        "classification and image reference.",
                        block_id=block_id, beat_ids=[beat_id],
                    ))
                else:
                    cta_covered_anchors.append(beat_id)
            if protected and not ebook_available:
                issues.append(_issue(
                    "CTA_COVER_UNAVAILABLE",
                    "No authentic ebook cover is configured. Remove ebook-specific "
                    "CTA metadata and references; never fabricate a cover. "
                    "If this is a real ebook promotion, configure its real cover "
                    "before producing the video.",
                    block_id=block_id, beat_ids=[beat_id],
                ))
            issues.extend(_validate_visual(beat, block_id=block_id, protected=protected))

            if beat_id in protected_cta_beats:
                expected = protected_cta_beats[beat_id]
                # No restructuring or visual mutation of protected CTA IDs.
                for key, value in expected.items():
                    if beat.get(key) != value:
                        issues.append(_issue(
                            "CTA_PROTECTED", f"Protected CTA field changed: {key}.",
                            block_id=block_id, beat_ids=[beat_id],
                        ))
            kind = beat.get("visual_type")
            if (kind == "media_image"
                    and beat.get("generation_mode") == "grid"
                    and beat.get("cta_group_id") is None
                    and isinstance(beat.get("image_prompts"), list)):
                grid_assets.extend([beat_id] * len(beat["image_prompts"]))
            else:
                finish_grid_run()
            avatar_count += kind == "avatar"
            avatar_media_count += kind == "avatar_media"
            if beat.get("audio") is True:
                current_run.append(beat_id)
                spoken_chars += len(text.strip())
            else:
                finish_run()

            image_count = (
                len(beat["image_prompts"])
                if kind == "media_image" and isinstance(beat.get("image_prompts"), list)
                else 1
            )
            records.append({
                "block_id": block_id, "beat_id": beat_id,
                "visual_type": kind, "image_count": image_count,
                "source_start": cursor + len(reconstructed) - len(text),
                "source_end": cursor + len(reconstructed),
            })
        finish_grid_run()
        if reconstructed != group["text"]:
            issues.append(_issue(
                "SOURCE_INTEGRITY",
                "Concatenated beat text does not exactly equal the materialized source group.",
                block_id=block_id,
                details={"expected_characters": len(group["text"]),
                         "actual_characters": len(reconstructed)},
            ))
        if group["type"] == "close":
            last = beats[-1]
            if not isinstance(last, dict) or (
                last.get("visual_type") != "avatar"
                or last.get("prompt") is not None
                or last.get("video_prompt") is not None
                or last.get("reference") is not False
                or last.get("audio") is not True
            ):
                issues.append(_issue(
                    "CLOSE_AVATAR", "The last close beat must reuse the original avatar.",
                    block_id=block_id,
                ))
        cursor += len(group["text"])
    finish_run()

    if authentic_cta_required and not cta_covered_anchors:
        issues.append(_issue(
            "CTA_SPOKEN_OFFER_MISSING",
            "The source contains a real ebook QR promotion but the "
            "submitted audiovisual plan does not classify its spoken "
            "promotional beats as an authentic ebook CTA.",
        ))
    missing_protected = set(protected_cta_beats) - all_ids
    if missing_protected:
        issues.append(_issue(
            "CTA_PROTECTED", "Protected CTA beats were deleted or renamed.",
            beat_ids=sorted(missing_protected),
        ))
    total_beats = len(records)
    avatar_pct = avatar_count / total_beats * 100 if total_beats else 0.0
    if total_beats == 0:
        issues.append(_issue("NO_BEATS", "Plan has no valid beats."))
    elif avatar_count * 100 >= AVATAR_MAX_PERCENT * total_beats:
        issues.append(_issue(
            "AVATAR_RATIO", f"Avatar ratio is {avatar_pct:.2f}%; must be below 45%.",
            details={"avatar_beats": avatar_count, "total_beats": total_beats},
        ))
    rhythm = _rhythm_map(records, script)
    return {
        "status": _status(issues),
        "source_sha256": sha256(script.encode("utf-8")).hexdigest(),
        "summary": {
            "total_beats": total_beats,
            "avatar_beats": avatar_count,
            "avatar_media_beats": avatar_media_count,
            "avatar_percentage": round(avatar_pct, 3),
            "max_spoken_run_characters": max(
                (x["characters"] for x in spoken_runs), default=0
            ),
            "speaking_runs": spoken_runs,
        },
        "issues": issues,
        "rhythm_map": rhythm,
    }
