"""Flat, external, secret-safe credential storage for YouTube channels."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import YouTubeConfigurationError, YouTubeCredentialsError

_CLIENT_SECRET = "client_secret.json"
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_UNSAFE = re.compile(r'[\\/<>:"|?*\x00-\x1f\x7f]')


def validate_channel_name(name: str) -> str:
    """Return a safe human-readable local selector (not a verified YouTube ID)."""
    if not isinstance(name, str):
        raise YouTubeConfigurationError("YouTube channel name must be text")
    value = name.strip()
    if (
        not value
        or value in {".", ".."}
        or value.endswith(".")
        or value.casefold().endswith(".json")
        or _UNSAFE.search(value)
        or any(unicodedata.category(char) == "Cc" for char in value)
        or value.upper().split(".", 1)[0].rstrip(" .") in _WINDOWS_RESERVED
        or value.casefold() == "client_secret"
    ):
        raise YouTubeConfigurationError("Invalid YouTube channel name")
    return value


def _checkout_root(path: Path) -> Path | None:
    """Find a project checkout, even when this package was installed elsewhere."""
    for parent in (path, *path.parents):
        if (parent / "pyproject.toml").is_file() and (parent / ".gitignore").is_file():
            return parent.resolve()
    return None


def _reject_linked_ancestors(path: Path) -> None:
    """Resolve none of the path until symlink and Windows junction checks pass."""
    for segment in (path, *path.parents):
        if segment.is_symlink() or segment.is_junction():
            raise YouTubeConfigurationError(
                "YouTube keystore path must not traverse symlinks or Windows junctions"
            )


class YouTubeStore:
    """Operate only on direct children of an external, flat keystore directory."""

    def __init__(self, directory: Path | str, *, repo_root: Path | None = None) -> None:
        if not str(directory).strip():
            raise YouTubeConfigurationError("YOUTUBE_KEYSTORE_DIR is not configured")
        # On Windows this expands e.g. %USERPROFILE%; on Linux it expands $HOME.
        original = Path(os.path.expandvars(str(directory))).expanduser()
        if not original.is_absolute():
            raise YouTubeConfigurationError("YOUTUBE_KEYSTORE_DIR must be an absolute path")
        _reject_linked_ancestors(original)
        resolved = original.resolve(strict=False)
        roots = (
            (repo_root.resolve(),) if repo_root is not None else
            tuple(
                root for root in (
                    _checkout_root(Path(__file__).resolve()),
                    _checkout_root(Path.cwd().resolve()),
                ) if root is not None
            )
        )
        for repository in roots:
            if resolved == repository or resolved.is_relative_to(repository):
                raise YouTubeConfigurationError(
                    "YouTube keystore must be outside the repository"
                )
        self.directory = resolved

    def ensure_directory(self) -> Path:
        _reject_linked_ancestors(self.directory)
        if self.directory.exists() and not self.directory.is_dir():
            raise YouTubeConfigurationError("YouTube keystore path is not a directory")
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name == "posix" and self.directory.stat().st_mode & 0o077:
                raise YouTubeConfigurationError(
                    "YouTube keystore directory needs owner-only permissions (chmod 700)"
                )
        except OSError:
            raise YouTubeConfigurationError("Cannot initialize YouTube keystore") from None
        return self.directory

    def client_secret_path(self, *, required: bool = True) -> Path:
        path = self.directory / _CLIENT_SECRET
        if path.is_symlink() or path.is_junction() or (path.exists() and not path.is_file()):
            raise YouTubeConfigurationError("Invalid YouTube client_secret.json")
        if required and not path.is_file():
            raise YouTubeConfigurationError("YouTube client_secret.json is missing")
        return path

    def token_path(self, channel: str, *, required: bool = False) -> Path:
        """Case-insensitive lookup preserves the existing filename's spelling."""
        name = validate_channel_name(channel)
        if self.directory.is_symlink() or self.directory.is_junction() or (
            self.directory.exists() and not self.directory.is_dir()
        ):
            raise YouTubeConfigurationError("Invalid YouTube keystore directory")
        requested = unicodedata.normalize("NFC", name + ".json").casefold()
        matches = [
            entry for entry in self.directory.iterdir()
            if unicodedata.normalize("NFC", entry.name).casefold() == requested
        ] if self.directory.is_dir() else []
        if len(matches) > 1:
            raise YouTubeConfigurationError("Conflicting YouTube channel token filenames")
        path = matches[0] if matches else self.directory / (name + ".json")
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise YouTubeConfigurationError("Invalid YouTube channel token file")
        if required and not path.is_file():
            raise YouTubeCredentialsError("YouTube channel is not authorized")
        return path

    def load_token(self, channel: str) -> dict[str, Any]:
        path = self.token_path(channel, required=True)
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(content, dict) or not content:
                raise ValueError("Invalid token")
        except (OSError, ValueError, UnicodeError):
            raise YouTubeCredentialsError(
                "Invalid YouTube token; reauthorize the channel"
            ) from None
        return content

    def save_token(self, channel: str, token: Mapping[str, Any]) -> Path:
        """Write safely without ever logging or returning credential contents."""
        if not isinstance(token, Mapping) or not token:
            raise YouTubeCredentialsError("Cannot save an empty YouTube token")
        self.ensure_directory()
        destination = self.token_path(channel)
        try:
            data = json.dumps(dict(token), ensure_ascii=False, indent=2) + "\n"
        except (TypeError, ValueError):
            raise YouTubeCredentialsError("Invalid YouTube token") from None
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=".youtube-token-", suffix=".tmp", dir=self.directory
            )
            temporary = Path(name)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Refuse a late symlink replacement rather than following it.
            if destination.is_symlink() or destination.is_junction():
                raise YouTubeConfigurationError("Unsafe YouTube channel token destination")
            os.replace(temporary, destination)
        except OSError:
            raise YouTubeCredentialsError("Could not persist YouTube credentials") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return destination

    def list_channels(self) -> tuple[str, ...]:
        _reject_linked_ancestors(self.directory)
        if not self.directory.is_dir():
            return ()
        names: dict[str, str] = {}
        for file in self.directory.iterdir():
            if file.suffix.casefold() != ".json" or file.name.casefold() == _CLIENT_SECRET:
                continue
            if file.is_symlink() or file.is_junction() or not file.is_file():
                raise YouTubeConfigurationError("Unsafe YouTube channel token entry")
            channel = validate_channel_name(file.name[:-5])
            key = unicodedata.normalize("NFC", channel).casefold()
            if key in names:
                raise YouTubeConfigurationError("Conflicting YouTube channel token filenames")
            names[key] = channel
        return tuple(sorted(names.values(), key=str.casefold))
