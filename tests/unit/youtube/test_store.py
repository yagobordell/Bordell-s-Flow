"""Safe flat credential store; tests use fake data and temporary external directories."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_video_factory.integrations.youtube.errors import (
    YouTubeConfigurationError,
    YouTubeCredentialsError,
)
from ai_video_factory.integrations.youtube.store import YouTubeStore, validate_channel_name


@pytest.mark.parametrize("name,expected", [
    ("  Canal Español  ", "Canal Español"),
    ("История @ oficial", "История @ oficial"),
    ("my.channel_2", "my.channel_2"),
])
def test_human_channel_names(name: str, expected: str) -> None:
    assert validate_channel_name(name) == expected


@pytest.mark.parametrize("name", [
    "", "  ", ".", "..", "../other", r"..\\other", "a/b", "a\\b",
    "A.json", "CON", "con.txt", "PRN", "LPT9", "COM1",
    "bad:name", "bad?name", "bad*name", "bad<name", "bad>name",
    "bad|name", 'bad"name', "trailing.", "client_secret", "CON .other", "a\x00b", "a\x85b",
])
def test_unsafe_channel_names(name: str) -> None:
    with pytest.raises(YouTubeConfigurationError):
        validate_channel_name(name)


def test_keystore_must_be_outside_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(YouTubeConfigurationError):
        YouTubeStore(repo / "config" / "youtube", repo_root=repo)


def test_keystore_works_outside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    store = YouTubeStore(tmp_path / "external", repo_root=repository)
    assert store.ensure_directory().is_dir()
    assert store.client_secret_path(required=False) == tmp_path / "external/client_secret.json"
    with pytest.raises(YouTubeConfigurationError, match="missing"):
        store.client_secret_path()


def test_keystore_refuses_existing_file(tmp_path: Path) -> None:
    root = tmp_path / "keys"
    root.write_text("not-a-directory")
    store = YouTubeStore(root)
    with pytest.raises(YouTubeConfigurationError):
        store.ensure_directory()


def test_case_insensitive_lookup_preserves_original_filename(tmp_path: Path) -> None:
    store = YouTubeStore(tmp_path / "keys")
    store.save_token("Canal Principal", {"token": "FAKE_EXAMPLE_ONLY"})
    assert store.token_path("CANAL PRINCIPAL").name == "Canal Principal.json"
    store.save_token("canal principal", {"token": "FAKE_EXAMPLE_ONLY_2"})
    assert store.list_channels() == ("Canal Principal",)
    assert store.load_token("CANAL PRINCIPAL")["token"] == "FAKE_EXAMPLE_ONLY_2"
    # Windows resolves differently cased names to the same on-disk file.
    # Compare directory entry spelling rather than case-insensitive exists().
    assert "canal principal.json" not in {p.name for p in store.directory.iterdir()}


def test_duplicate_casefold_names_are_rejected(tmp_path: Path) -> None:
    store = YouTubeStore(tmp_path / "keys")
    store.ensure_directory()
    (store.directory / "One.json").write_text("{}")
    (store.directory / "one.json").write_text("{}")
    # Linux can create both, while Windows cannot: both platforms must refuse collisions.
    if len(tuple(store.directory.glob("*.json"))) == 2:
        with pytest.raises(YouTubeConfigurationError, match="Conflicting"):
            store.token_path("ONE")
        with pytest.raises(YouTubeConfigurationError, match="Conflicting"):
            store.list_channels()


def test_store_writes_atomically_and_cleans_tempfiles(tmp_path: Path) -> None:
    store = YouTubeStore(tmp_path / "keys")
    with patch("ai_video_factory.integrations.youtube.store.os.replace", wraps=os.replace) as move:
        destination = store.save_token("Canal 🦉", {"token": "FAKE_EXAMPLE_ONLY"})
    assert move.call_count == 1
    assert destination.is_file()
    assert store.load_token("Canal 🦉") == {"token": "FAKE_EXAMPLE_ONLY"}
    assert list(store.directory.glob(".youtube-token-*.tmp")) == []
    if os.name == "posix":
        assert destination.stat().st_mode & 0o077 == 0


def test_failed_save_removes_tempfile_without_leaking_token(tmp_path: Path) -> None:
    store = YouTubeStore(tmp_path / "keys")
    secret = "FAKE_SENSITIVE_TOKEN_FOR_TEST_ONLY"
    with patch(
        "ai_video_factory.integrations.youtube.store.os.replace",
        side_effect=OSError(secret),
    ):
        with pytest.raises(YouTubeCredentialsError) as error:
            store.save_token("Canal", {"refresh_token": secret})
    assert secret not in str(error.value)
    assert list(store.directory.iterdir()) == []


def test_invalid_token_does_not_echo_contents(tmp_path: Path) -> None:
    store = YouTubeStore(tmp_path / "keys")
    store.ensure_directory()
    secret = "FAKE_SENSITIVE_TOKEN_FOR_TEST_ONLY"
    (store.directory / "Canal.json").write_text('{"refresh_token":"' + secret)
    with pytest.raises(YouTubeCredentialsError) as error:
        store.load_token("Canal")
    assert secret not in str(error.value)


def test_store_rejects_symlink_tokens_and_keystore(tmp_path: Path) -> None:
    external = tmp_path / "secret.json"
    external.write_text('{"token":"FAKE_EXAMPLE_ONLY"}')
    store = YouTubeStore(tmp_path / "keys")
    store.ensure_directory()
    try:
        (store.directory / "Canal.json").symlink_to(external)
    except (NotImplementedError, OSError):
        pytest.skip("Symlinks unavailable on this platform")
    with pytest.raises(YouTubeConfigurationError):
        store.load_token("Canal")
    with pytest.raises(YouTubeConfigurationError):
        store.save_token("Canal", {"token": "FAKE_EXAMPLE_ONLY"})
    (tmp_path / "link").symlink_to(store.directory)
    with pytest.raises(YouTubeConfigurationError):
        YouTubeStore(tmp_path / "link", repo_root=tmp_path / "notrepo")
