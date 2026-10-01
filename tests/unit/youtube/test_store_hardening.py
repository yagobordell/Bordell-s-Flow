"""External keystore path and filesystem permission hardening tests."""

import os
from pathlib import Path

import pytest

from ai_video_factory.integrations.youtube.errors import YouTubeConfigurationError
from ai_video_factory.integrations.youtube.store import YouTubeStore


def test_relative_keystore_is_rejected() -> None:
    with pytest.raises(YouTubeConfigurationError, match="absolute"):
        YouTubeStore("project-credentials")


def test_system_environment_variable_is_expanded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_EXTERNAL_TEST_ROOT", str(tmp_path))
    variable = (
        "%YOUTUBE_EXTERNAL_TEST_ROOT%" if os.name == "nt"
        else "$YOUTUBE_EXTERNAL_TEST_ROOT"
    )
    store = YouTubeStore(variable + os.sep + "youtube")
    assert store.directory == (tmp_path / "youtube").resolve()


def test_current_checkout_is_rejected_even_when_package_runs_elsewhere(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='example'\n")
    (repo / ".gitignore").write_text("credentials\n")
    monkeypatch.chdir(repo)
    with pytest.raises(YouTubeConfigurationError, match="outside"):
        YouTubeStore(repo / "some-external-looking-directory")


def test_external_keystore_created_owner_only(tmp_path: Path) -> None:
    store = YouTubeStore(tmp_path / "secret-folder")
    store.ensure_directory()
    if os.name == "posix":
        assert store.directory.stat().st_mode & 0o077 == 0


def test_existing_insecure_permissions_rejected_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX-only permission bits")
    path = tmp_path / "keys"
    path.mkdir(mode=0o755)
    path.chmod(0o755)
    store = YouTubeStore(path)
    with pytest.raises(YouTubeConfigurationError, match="owner-only"):
        store.ensure_directory()
    path.chmod(0o700)
    assert store.ensure_directory() == path


def test_symlinked_parent_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Unable to create symlink on this platform")
    with pytest.raises(YouTubeConfigurationError, match="symlinks"):
        YouTubeStore(link / "keys")
