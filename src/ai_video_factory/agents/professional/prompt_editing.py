"""Workspace-scoped prompt editing tools for the Professional feedback agent."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from ai_video_factory.agents.professional.prompt_loader import operational_prompt_paths

_PROMPT_ROOT = Path(__file__).resolve().parent / "prompts"


class PromptInstructionEditor:
    """Apply SDK patch operations only to active Professional instruction files."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or _PROMPT_ROOT).resolve()
        self.allowed_files = frozenset(operational_prompt_paths())
        self._changes: list[dict[str, str]] = []

    def _resolve(self, value: str) -> tuple[str, Path]:
        if not isinstance(value, str) or not value:
            raise ValueError("Prompt path must be a nonempty relative path.")
        relative = PurePosixPath(value)
        normalized = relative.as_posix()
        if (
            relative.is_absolute()
            or normalized != value
            or normalized not in self.allowed_files
        ):
            raise ValueError(f"Prompt path is not an active editable instruction: {value!r}")
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Prompt path contains a symbolic link: {value}")
        resolved = current.resolve()
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise ValueError(f"Prompt file is missing or outside the prompt root: {value}")
        return normalized, resolved

    def read(self, value: str) -> str:
        """Read the live contents of one allowlisted instruction file."""
        _, path = self._resolve(value)
        return path.read_bytes().decode("utf-8")

    def create_file(self, operation: Any) -> Any:
        from agents.editor import ApplyPatchResult

        return ApplyPatchResult(
            status="failed", output="Creating instruction files is not allowed."
        )

    def delete_file(self, operation: Any) -> Any:
        from agents.editor import ApplyPatchResult

        return ApplyPatchResult(
            status="failed", output="Deleting instruction files is not allowed."
        )

    def update_file(self, operation: Any) -> Any:
        from agents import apply_diff
        from agents.editor import ApplyPatchResult

        try:
            relative, path = self._resolve(operation.path)
            if operation.type != "update_file":
                raise ValueError("Only update_file operations are allowed.")
            if operation.move_to:
                raise ValueError("Moving instruction files is not allowed.")
            if not operation.diff:
                raise ValueError("The patch has no diff content.")
            if any(line.startswith((
                "*** Add File:", "*** Delete File:", "*** Move to:",
            )) for line in operation.diff.splitlines()):
                raise ValueError("Patches cannot create, delete, or move instruction files.")

            original_bytes = path.read_bytes()
            original = original_bytes.decode("utf-8")
            newline = "\r\n" if "\r\n" in original else "\n"
            normalized = original.replace("\r\n", "\n")
            updated = apply_diff(normalized, operation.diff)
            if updated == normalized:
                return ApplyPatchResult(status="completed", output=f"No change: {relative}")
            if not updated.strip():
                raise ValueError("Instruction files cannot be emptied.")
            if path.read_bytes() != original_bytes:
                raise ValueError(f"Instruction changed during patching; reread {relative}.")

            updated_bytes = updated.replace("\n", newline).encode("utf-8")
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=path.name + ".",
                    suffix=".tmp",
                    dir=path.parent,
                    delete=False,
                ) as temporary:
                    temporary.write(updated_bytes)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = Path(temporary.name)
                os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
                if path.read_bytes() != original_bytes:
                    raise ValueError(f"Instruction changed during patching; reread {relative}.")
                os.replace(temporary_path, path)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

            self._changes.append({
                "file": relative,
                "before_sha256": hashlib.sha256(original_bytes).hexdigest(),
                "after_sha256": hashlib.sha256(updated_bytes).hexdigest(),
                "diff": operation.diff,
            })
            return ApplyPatchResult(status="completed", output=f"Updated {relative}")
        except Exception as exc:
            return ApplyPatchResult(status="failed", output=str(exc))

    def take_changes(self) -> list[dict[str, str]]:
        changes = self._changes
        self._changes = []
        return changes


def build_prompt_instruction_tools(editor: PromptInstructionEditor) -> list[Any]:
    """Bind a live read function and the SDK's standard apply_patch tool."""
    from agents import ApplyPatchTool, function_tool

    @function_tool
    def read_prompt_instruction(file: str) -> str:
        """Read the current contents of an active Professional instruction MD.

        `file` must be one of the relative paths supplied in the feedback input.
        """
        try:
            return editor.read(file)
        except (OSError, ValueError) as exc:
            return f"Unable to read instruction file: {exc}"

    return [
        read_prompt_instruction,
        ApplyPatchTool(editor=editor, needs_approval=False),
    ]
