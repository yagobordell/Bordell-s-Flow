"""Pinned allowlisted English instructions for autonomous Agent B production."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

PRODUCTION_DOCUMENTS = (
    "core.md",
    "workflow.md",
    "creative_principles.md",
    "segmentation_and_pacing.md",
    "narrative_continuity.md",
    "visual_direction.md",
    "motion_direction.md",
    "references/image_prompting.md",
)
_DEFAULT_ROOT = Path(__file__).resolve().parent / "instructions"


@dataclass(frozen=True)
class InstructionBundle:
    contents: dict[str, str]
    sha256: str

    @classmethod
    def load(cls, root: Path | None = None) -> InstructionBundle:
        directory = (root or _DEFAULT_ROOT).resolve(strict=True)
        contents: dict[str, str] = {}
        digest = hashlib.sha256()
        for name in PRODUCTION_DOCUMENTS:
            path = directory.joinpath(name)
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(directory):
                raise ValueError(f"Instruction file escapes protected directory: {name}")
            body = path.read_text(encoding="utf-8")
            if not body.strip():
                raise ValueError(f"Instruction file is empty: {name}")
            contents[name] = body
            digest.update(name.encode("utf-8") + b"\x00" + body.encode("utf-8") + b"\x00")
        return cls(contents, digest.hexdigest())

    def get(self, name: str, offset: int = 0, limit: int = 12000) -> dict[str, object]:
        if name not in self.contents:
            raise ValueError("Document is not available to this production agent.")
        if offset < 0 or not 1 <= limit <= 20000:
            raise ValueError("Invalid instruction range.")
        data = self.contents[name]
        return {"document": name, "offset": offset, "text": data[offset:offset + limit],
                "next_offset": min(len(data), offset + limit), "total_characters": len(data),
                "bundle_sha256": self.sha256}

    def starting_instructions(self) -> str:
        return "\n\n".join((
            self.contents["core.md"],
            self.contents["workflow.md"],
            "The other pinned creative instructions and official image-prompting guidance "
            "are available through read_instructions. Consult them when relevant. "
            "Never load human-facing Spanish design documentation into model instructions. "
            "Use tools to inspect the full script, create and validate one final "
            "professional visual plan. Report completion only after saving it.",
        ))
