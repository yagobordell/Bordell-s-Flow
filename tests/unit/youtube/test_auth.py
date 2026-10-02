"""Phase 2 OAuth tests: no browser, network or genuine credential material."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from ai_video_factory.integrations.youtube import auth
from ai_video_factory.integrations.youtube.errors import (
    YouTubeAuthenticationError,
    YouTubeConfigurationError,
    YouTubeCredentialsError,
)
from ai_video_factory.integrations.youtube.store import YouTubeStore

SCOPE = auth.YOUTUBE_UPLOAD_SCOPE
DUMMY = {
    "client_id": "DUMMY_OAUTH_CLIENT_ID_FOR_TESTS",
    "client_secret": "DUMMY_OAUTH_CLIENT_SECRET_FOR_TESTS",
    "refresh_token": "DUMMY_REFRESH_TOKEN_FOR_TESTS",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": [SCOPE],
    "token": "DUMMY_ACCESS_TOKEN_FOR_TESTS",
}


def store_with_client(tmp_path: Path) -> YouTubeStore:
    store = YouTubeStore(tmp_path / "outside")
    store.ensure_directory()
    (store.directory / "client_secret.json").write_text(
        json.dumps(
            {"installed": {
                "client_id": DUMMY["client_id"],
                "client_secret": DUMMY["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": DUMMY["token_uri"],
                "redirect_uris": ["http://localhost"],
            }}
        ),
        encoding="utf-8",
    )
    return store


class FakeCredentials:
    def __init__(self, *, valid: bool = True, refresh_token: str | None = DUMMY["refresh_token"]):
        self.info = dict(DUMMY)
        self.valid = valid
        self.refresh_token = refresh_token
        self.granted_scopes: list[str] | None = [SCOPE]
        self.refresh_exception: Exception | None = None
        self.refresh_calls = 0

    def refresh(self, request: Any) -> None:
        self.refresh_calls += 1
        if self.refresh_exception:
            raise self.refresh_exception
        self.info["token"] = "DUMMY_NEW_ACCESS_TOKEN_FOR_TESTS"
        self.valid = True
        if self.refresh_token is not None:
            self.info["refresh_token"] = self.refresh_token

    def to_json(self) -> str:
        self.info["refresh_token"] = self.refresh_token
        return json.dumps(self.info)


def test_authorize_only_upload_scope_and_pkce(tmp_path: Path) -> None:
    store = store_with_client(tmp_path)
    credential = FakeCredentials()
    flow = Mock()
    flow.run_local_server.return_value = credential
    factory = Mock(return_value=flow)

    auth.authorize_channel("Mi Canal", store, flow_factory=factory)

    assert store.load_token("Mi Canal")["refresh_token"] == DUMMY["refresh_token"]
    args, kwargs = factory.call_args
    assert args == (str(store.directory / "client_secret.json"),)
    assert kwargs == {"scopes": [SCOPE], "autogenerate_code_verifier": True}
    flow.run_local_server.assert_called_once_with(
        host="localhost", port=0, open_browser=True,
        authorization_prompt_message=None,
        success_message="Authorization received. You may close this browser window.",
        access_type="offline", prompt="consent",
    )


def test_existing_authorization_requires_explicit_reauthorization(tmp_path: Path) -> None:
    store = store_with_client(tmp_path)
    store.save_token("Canal", DUMMY)
    factory = Mock()
    with pytest.raises(YouTubeCredentialsError, match="already has credentials"):
        auth.authorize_channel("Canal", store, flow_factory=factory)
    factory.assert_not_called()
    assert store.load_token("Canal") == DUMMY


@pytest.mark.parametrize("case", ["no_refresh_token", "no_permission", "provider_exception"])
def test_failed_reauthorization_preserves_original_token(tmp_path: Path, case: str) -> None:
    store = store_with_client(tmp_path)
    store.save_token("Canal", DUMMY)
    old = (store.directory / "Canal.json").read_bytes()
    flow = Mock()
    candidate = FakeCredentials()
    if case == "no_refresh_token":
        candidate.refresh_token = None
    elif case == "no_permission":
        candidate.granted_scopes = ["https://www.googleapis.com/auth/youtube.readonly"]
    else:
        flow.run_local_server.side_effect = RuntimeError(
            "DUMMY_SHOULD_NOT_LEAK_REFRESH_TOKEN_FOR_TESTS"
        )
    if case != "provider_exception":
        flow.run_local_server.return_value = candidate
    with pytest.raises(YouTubeAuthenticationError) as error:
        auth.authorize_channel("Canal", store, reauthorize=True, flow_factory=Mock(
            return_value=flow
        ))
    assert "DUMMY_SHOULD_NOT_LEAK" not in str(error.value)
    assert (store.directory / "Canal.json").read_bytes() == old


@pytest.mark.parametrize("payload", [
    {"web": dict(client_id="FAKE", client_secret="FAKE")},
    {"installed": {"client_id": "FAKE"}},
    [],
    {},
])
def test_invalid_desktop_client_rejected(tmp_path: Path, payload: object) -> None:
    store = store_with_client(tmp_path)
    (store.directory / "client_secret.json").write_text(json.dumps(payload))
    factory = Mock()
    with pytest.raises(YouTubeConfigurationError, match="Invalid Desktop"):
        auth.authorize_channel("Canal", store, flow_factory=factory)
    factory.assert_not_called()
    assert store.list_channels() == ()


def test_refresh_is_noninteractive_and_preserves_refresh_token(tmp_path: Path, monkeypatch) -> None:
    store = YouTubeStore(tmp_path / "outside")
    store.save_token("Canal", DUMMY)
    fake = FakeCredentials(valid=False)
    monkeypatch.setattr(auth, "_google_credentials", lambda info: fake)
    request_factory = Mock(return_value=object())

    loaded = auth.load_credentials("Canal", store, request_factory=request_factory)

    assert loaded is fake
    assert fake.refresh_calls == 1
    request_factory.assert_called_once()
    assert store.load_token("Canal")["refresh_token"] == DUMMY["refresh_token"]
    assert store.load_token("Canal")["token"] == "DUMMY_NEW_ACCESS_TOKEN_FOR_TESTS"


def test_valid_credentials_never_refresh(tmp_path: Path, monkeypatch) -> None:
    store = YouTubeStore(tmp_path / "outside")
    store.save_token("Canal", DUMMY)
    fake = FakeCredentials(valid=True)
    monkeypatch.setattr(auth, "_google_credentials", lambda info: fake)
    request_factory = Mock(side_effect=AssertionError("Never request network"))

    assert auth.load_credentials("Canal", store, request_factory=request_factory) is fake
    request_factory.assert_not_called()
    assert fake.refresh_calls == 0


def test_refresh_failure_redacts_provider_exception_and_preserves_file(
    tmp_path: Path, monkeypatch
) -> None:
    store = YouTubeStore(tmp_path / "outside")
    store.save_token("Canal", DUMMY)
    prior = store.token_path("Canal").read_bytes()
    fake = FakeCredentials(valid=False)
    fake.refresh_exception = RuntimeError("DUMMY_SHOULD_NOT_LEAK_PROVIDER_TOKEN")
    monkeypatch.setattr(auth, "_google_credentials", lambda info: fake)
    with pytest.raises(YouTubeAuthenticationError) as error:
        auth.load_credentials("Canal", store, request_factory=lambda: object())
    assert error.value.__cause__ is None
    assert "DUMMY_SHOULD_NOT_LEAK_PROVIDER_TOKEN" not in str(error.value)
    assert store.token_path("Canal").read_bytes() == prior


def test_missing_refresh_or_scopes_never_contact_google(tmp_path: Path, monkeypatch) -> None:
    store = YouTubeStore(tmp_path / "outside")
    google = Mock()
    monkeypatch.setattr(auth, "_google_credentials", google)
    for changes in ({"refresh_token": ""}, {"scopes": []}, {"scopes": []}):
        store.save_token("Canal", {**DUMMY, **changes})
        with pytest.raises(YouTubeCredentialsError):
            auth.load_credentials("Canal", store)
    google.assert_not_called()


def test_check_without_client_secret_needs_no_browser(tmp_path: Path, monkeypatch) -> None:
    store = YouTubeStore(tmp_path / "outside")
    store.save_token("Canal", DUMMY)
    fake = FakeCredentials(valid=True)
    monkeypatch.setattr(auth, "_google_credentials", lambda info: fake)
    assert auth.load_credentials("Canal", store) is fake
    assert not (store.directory / "client_secret.json").exists()
