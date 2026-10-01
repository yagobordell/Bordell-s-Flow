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


class YouTubeStore:
    """Operate only on direct children of an external, flat keystore directory."""

    def __init__(self, directory: Path | str, *, repo_root: Path | None = None) -> None:
        if not str(directory).strip():
            raise YouTubeConfigurationError("YOUTUBE_KEYSTORE_DIR is not configured")
        original = Path(directory).expanduser()
        if original.is_symlink():
            raise YouTubeConfigurationError("YouTube keystore must not be a symbolic link")
        resolved = original.resolve(strict=False)
        repository = (repo_root or Path(__file__).resolve().parents[4]).resolve()
        if resolved == repository or resolved.is_relative_to(repository):
            raise YouTubeConfigurationError("YouTube keystore must be outside the repository")
        self.directory = resolved

    def ensure_directory(self) -> Path:
        if self.directory.is_symlink():
            raise YouTubeConfigurationError("YouTube keystore must not be a symbolic link")
        if self.directory.exists() and not self.directory.is_dir():
            raise YouTubeConfigurationError("YouTube keystore path is not a directory")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise YouTubeConfigurationError("Cannot initialize YouTube keystore") from None
        return self.directory

    def client_secret_path(self, *, required: bool = True) -> Path:
        path = self.directory / _CLIENT_SECRET
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise YouTubeConfigurationError("Invalid YouTube client_secret.json")
        if required and not path.is_file():
            raise YouTubeConfigurationError("YouTube client_secret.json is missing")
        return path

    def token_path(self, channel: str, *, required: bool = False) -> Path:
        """Case-insensitive lookup preserves the existing filename's spelling."""
        name = validate_channel_name(channel)
        if self.directory.is_symlink() or (
            self.directory.exists() and not self.directory.is_dir()
        ):
            raise YouTubeConfigurationError("Invalid YouTube keystore directory")
        requested = (name + ".json").casefold()
        matches = [
            entry for entry in self.directory.iterdir()
            if entry.name.casefold() == requested
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
            if destination.is_symlink():
                raise YouTubeConfigurationError("Unsafe YouTube channel token destination")
            os.replace(temporary, destination)
        except OSError:
            raise YouTubeCredentialsError("Could not persist YouTube credentials") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return destination

    def list_channels(self) -> tuple[str, ...]:
        if not self.directory.is_dir():
            return ()
        names: dict[str, str] = {}
        for file in self.directory.iterdir():
            if file.suffix.casefold() != ".json" or file.name.casefold() == _CLIENT_SECRET:
                continue
            if file.is_symlink() or not file.is_file():
                raise YouTubeConfigurationError("Unsafe YouTube channel token entry")
            channel = validate_channel_name(file.name[:-5])
            if channel.casefold() in names:
                raise YouTubeConfigurationError("Conflicting YouTube channel token filenames")
            names[channel.casefold()] = channel
        return tuple(sorted(names.values(), key=str.casefold))
