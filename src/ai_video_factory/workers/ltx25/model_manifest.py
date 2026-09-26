from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_CHUNK_SIZE = 16 * 1024 * 1024


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def installed_model_manifest_ready(
    installed_path: Path,
    *,
    repository: str,
    revision: str,
    root: Path,
    expected_files: Sequence[str],
) -> bool:
    """Check the downloader's atomic, SHA-verified receipt without rehashing on /ready.

    This is a cheap readiness check, NOT a replacement for the full SHA-256
    validation performed by the downloader before publishing its receipt.
    A separate per-start completion marker is required by the worker so an
    old receipt cannot make a freshly restarted container ready prematurely.
    """
    if not installed_path.is_file() or not expected_files:
        return False
    try:
        installed: dict[str, Any] = json.loads(
            installed_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False

    if (
        installed.get("schema_version") != 1
        or installed.get("repository") != repository
        or installed.get("revision") != revision
    ):
        return False
    records = installed.get("files")
    if not isinstance(records, dict) or sorted(records) != sorted(expected_files):
        return False

    for relative in expected_files:
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or ".." in parts:
            return False
        record = records.get(relative)
        if not isinstance(record, dict):
            return False
        size = record.get("size")
        digest = record.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            return False
        path = root / relative
        try:
            if not path.is_file() or path.stat().st_size != size:
                return False
        except OSError:
            return False
    return True


def model_bootstrap_is_ready(
    *,
    root: Path,
    repository: str,
    revision: str,
    expected_files: Sequence[str],
    completion_marker: Path,
) -> bool:
    """Require this container start's completion signal and exact verified receipt."""
    try:
        if completion_marker.read_text(encoding="utf-8").strip() != revision:
            return False
    except OSError:
        return False
    return installed_model_manifest_ready(
        root / ".bordell-installed-model-manifest.json",
        repository=repository,
        revision=revision,
        root=root,
        expected_files=expected_files,
    )


def validate_installed_model_manifest(
    installed_path: Path,
    *,
    repository: str,
    revision: str,
    root: Path,
    expected_files: list[str],
) -> bool:
    if not installed_path.is_file():
        return False
    try:
        installed: dict[str, Any] = json.loads(
            installed_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False

    if installed.get("repository") != repository or installed.get("revision") != revision:
        return False

    records = installed.get("files")
    if not isinstance(records, dict) or sorted(records) != sorted(expected_files):
        return False

    for relative in expected_files:
        record = records.get(relative)
        if not isinstance(record, dict):
            return False
        path = root / relative
        try:
            stat = path.stat()
        except OSError:
            return False
        if not path.is_file() or stat.st_size <= 0:
            return False
        try:
            recorded_size = int(record.get("size", -1))
            recorded_sha256 = str(record["sha256"])
        except (KeyError, TypeError, ValueError):
            return False
        if recorded_size != stat.st_size or len(recorded_sha256) != 64:
            return False
        try:
            actual_sha256 = sha256_path(path)
        except OSError:
            return False
        if not hmac.compare_digest(recorded_sha256, actual_sha256):
            return False

    return True


def write_installed_model_manifest(
    installed_path: Path,
    *,
    repository: str,
    revision: str,
    root: Path,
    files: list[str],
) -> None:
    records: dict[str, dict[str, int | str]] = {}
    for relative in files:
        path = root / relative
        records[relative] = {
            "size": path.stat().st_size,
            "sha256": sha256_path(path),
        }

    payload = {
        "schema_version": 1,
        "repository": repository,
        "revision": revision,
        "files": records,
    }
    installed_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = installed_path.with_suffix(installed_path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, installed_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate LTX model cache provenance.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "write"):
        child = subparsers.add_parser(command)
        child.add_argument("--installed-path", type=Path, required=True)
        child.add_argument("--repository", required=True)
        child.add_argument("--revision", required=True)
        child.add_argument("--root", type=Path, required=True)
        child.add_argument("files", nargs="+")
    return parser


def main() -> None:
    args = _parser().parse_args()
    kwargs = {
        "repository": args.repository,
        "revision": args.revision,
        "root": args.root,
    }
    if args.command == "validate":
        valid = validate_installed_model_manifest(
            args.installed_path,
            expected_files=args.files,
            **kwargs,
        )
        raise SystemExit(0 if valid else 1)

    write_installed_model_manifest(
        args.installed_path,
        files=args.files,
        **kwargs,
    )
    print(
        f"MODEL_MANIFEST_WRITTEN path={args.installed_path} "
        f"files={len(args.files)}"
    )


if __name__ == "__main__":
    main()
