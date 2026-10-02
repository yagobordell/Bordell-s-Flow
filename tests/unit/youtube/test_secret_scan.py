"""Secret guard regression tests use only dummy values and temporary Git indexes."""

import json
import subprocess
from pathlib import Path

from scripts.security.check_youtube_secrets import scan


def _git_add(root: Path, name: str, contents: str) -> None:
    file = root / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(contents, encoding="utf-8")
    subprocess.run(["git", "add", "--", name], cwd=root, check=True, capture_output=True)


def test_fails_on_tracked_client_secret_name_and_oauth_json(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    _git_add(tmp_path, "config/client_secret.json", "{}")
    _git_add(
        tmp_path, "fixtures/creds.json",
        json.dumps({
            "client_id": "DUMMY_ONLY",
            "client_secret": "DUMMY_ONLY",
            "refresh_token": "DUMMY_ONLY",
        }),
    )
    _git_add(tmp_path, "fixtures/safe.json", '{"test": true}')
    assert scan(tmp_path) == ["config/client_secret.json", "fixtures/creds.json"]


def test_fails_on_tracked_credential_directory(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    _git_add(tmp_path, "keystores/youtube/config.json", "{}")
    assert scan(tmp_path) == ["keystores/youtube/config.json"]


def test_safe_tracked_docs_and_examples_pass(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    _git_add(tmp_path, ".env.example", "YOUTUBE_KEYSTORE_DIR=\n")
    _git_add(tmp_path, "docs/youtube.md", "Keep OAuth JSON outside the checkout.")
    assert scan(tmp_path) == []
