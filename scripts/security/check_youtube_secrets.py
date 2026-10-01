"""Defense-in-depth scan of tracked repository files; never print matched values.

This is not a replacement for keeping keystores outside Git or org-level
secret scanning. It rejects obvious credential files, OAuth JSON and several
high-confidence secret formats before a YouTube PR can pass CI.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PRIVATE_NAMES = {"client_secret.json", "youtube_token.json"}
PRIVATE_DIRECTORIES = {"youtube-keystore", "keystores"}
SECRET_PATTERNS = (
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"AIza[0-9A-Za-z_-]{25,}"),
    re.compile(rb"gh[pousr]_[0-9a-zA-Z]{30,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def _walk(value: object) -> bool:
    if isinstance(value, dict):
        keys = set(value)
        if {"client_id", "client_secret", "refresh_token"} <= keys:
            return True
        if "private_key" in keys and "client_email" in keys:
            return True
        if "installed" in keys or "web" in keys:
            client = value.get("installed", value.get("web"))
            if isinstance(client, dict) and "client_secret" in client:
                return True
        return any(_walk(nested) for nested in value.values())
    if isinstance(value, list):
        return any(_walk(nested) for nested in value)
    return False


def scan(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    errors: list[str] = []
    for raw_name in filter(None, result.stdout.split(b"\0")):
        name = raw_name.decode("utf-8", errors="surrogateescape")
        path = Path(name)
        if path.name.lower() in PRIVATE_NAMES or any(
            component.lower() in PRIVATE_DIRECTORIES for component in path.parts
        ):
            errors.append(name)
            continue
        if path.suffix == ".env" and not path.name.endswith(".example"):
            errors.append(name)
            continue
        if path.is_symlink() or not path.is_file():
            continue
        # Binary/media can be large; credential files should never be committed.
        if path.suffix.lower() not in {
            ".py", ".json", ".jsonl", ".yaml", ".yml", ".toml",
            ".md", ".rst", ".env", ".example", ".ps1", ".sh",
        }:
            continue
        data = (root / path).read_bytes()
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            errors.append(name)
            continue
        if path.suffix.lower() == ".json":
            try:
                if _walk(json.loads(data)):
                    errors.append(name)
            except (ValueError, UnicodeDecodeError):
                pass
    return sorted(set(errors))


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    flagged = scan(repo)
    if flagged:
        print("Tracked files may contain secrets; inspect/remove these paths:", file=sys.stderr)
        for path in flagged:
            print("  " + path, file=sys.stderr)
        return 1
    print("Tracked-file YouTube secret safety scan passed (defense-in-depth).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
