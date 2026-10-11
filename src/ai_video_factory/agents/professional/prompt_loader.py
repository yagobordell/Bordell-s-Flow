"""Phase-specific Markdown loader for a single OpenAI Professional agent.

Rule files are always explicitly added as model instructions. Memory is an
advisory layer and can be overridden from a persistent, user-reviewed folder.
Source snapshots are audit-only: old per-block instructions no longer apply.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

_BASE = ("agent.md", "rules/source_integrity.md")
_VISUAL = (
    "rules/visual_types.md",
    "rules/screens_documents.md",
    "rules/video_motion.md",
    "rules/continuity_grids.md",
    "rules/visual_review.md",
    "rules/professional_cta.md",
    "rules/editor_notes.md",
)
_PHASE: dict[str, tuple[str, ...]] = {
    "01_groups": (
        "rules/grouping.md", "phases/01_groups.md", "memory/groups.md",
    ),
    "02_planning": (
        "rules/beat_segmentation.md", *_VISUAL,
        "phases/02_planning.md", "memory/beats_visuals.md",
    ),
    "03_evaluation": (
        "rules/beat_segmentation.md", *_VISUAL,
        "phases/03_evaluation.md", "memory/beats_visuals.md",
    ),
    "04_correction": (
        "rules/beat_segmentation.md", *_VISUAL,
        "phases/04_correction.md", "memory/beats_visuals.md",
    ),
    "05_delivery": (
        "rules/visual_types.md", "rules/professional_cta.md",
        "phases/05_delivery.md",
    ),
}


def phase_paths(phase: str) -> tuple[str, ...]:
    """Return exact ordered files without using model-selected rule routing."""
    try:
        return (*_BASE, *_PHASE[phase])
    except KeyError as exc:
        raise ValueError(f"Unsupported Professional agent phase: {phase}") from exc


def load_phase_instructions(
    phase: str, *, memory_root: Path | None = None,
) -> str:
    """Compose explicit model-visible instructions for the requested phase."""
    root = files("ai_video_factory.agents.professional").joinpath("prompts")
    sections: list[str] = []
    for relative in phase_paths(phase):
        text: str
        if memory_root is not None and relative.startswith("memory/"):
            # Only these two fixed names can be read; never accept model paths.
            stored = memory_root / Path(relative).name
            text = stored.read_text(encoding="utf-8") if stored.is_file() else (
                root.joinpath(relative).read_text(encoding="utf-8")
            )
        else:
            text = root.joinpath(relative).read_text(encoding="utf-8")
        sections.append(f"--- BEGIN {relative} ---\n{text}\n--- END {relative} ---")
    return "\n\n".join(sections)


def load_post_generation_motion_instructions() -> str:
    """Use after final images and STT; not as part of the five planning phases."""
    root = files("ai_video_factory.agents.professional").joinpath("prompts")
    return "\n\n".join([
        root.joinpath("agent.md").read_text(encoding="utf-8"),
        root.joinpath("rules/post_generation_motion.md").read_text(encoding="utf-8"),
    ])
