"""Desktop OAuth and automatic token refresh, without YouTube channel-read scopes.

Only the interactive authorization method needs client_secret.json. Publication
uses the separate authorized-user token file and never opens a browser.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import (
    YouTubeAuthenticationError,
    YouTubeConfigurationError,
    YouTubeCredentialsError,
)
from .locking import channel_lock
from .store import YouTubeStore, validate_channel_name

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
_REQUIRED_CLIENT_FIELDS = ("client_id", "client_secret", "auth_uri", "token_uri")
_REQUIRED_TOKEN_FIELDS = ("client_id", "client_secret", "refresh_token", "token_uri")


def _oauth_client_file(store: YouTubeStore) -> Path:
    """Reject unsafe or malformed OAuth clients without exposing secret contents."""
    path = store.client_secret_path()
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("oversized")
        data = json.loads(path.read_text(encoding="utf-8"))
        installed = data["installed"]
        if not isinstance(installed, dict) or not all(
            isinstance(installed.get(key), str) and installed[key]
            for key in _REQUIRED_CLIENT_FIELDS
        ):
            raise ValueError("incomplete")
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise YouTubeConfigurationError(
            "Invalid Desktop OAuth client_secret.json; replace the file in the external keystore"
        ) from None
    # Credentials for a web application use a different redirect/authentication flow.
    return path


def _check_stored_info(info: dict[str, Any]) -> None:
    """Reject incomplete/wrong-scope data rather than assuming it was authorized."""
    if not all(isinstance(info.get(key), str) and info[key] for key in _REQUIRED_TOKEN_FIELDS):
        raise YouTubeCredentialsError("Incomplete YouTube credentials; reauthorize the channel")
    scopes = info.get("scopes")
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, (tuple, list)) or YOUTUBE_UPLOAD_SCOPE not in scopes:
        raise YouTubeCredentialsError(
            "YouTube upload permission cannot be verified; reauthorize the channel"
        )


def _google_credentials(info: dict[str, Any]) -> Any:
    try:
        from google.oauth2.credentials import Credentials
    except ImportError:
        raise YouTubeConfigurationError(
            "Install optional YouTube dependencies: uv sync --locked --extra youtube"
        ) from None
    try:
        return Credentials.from_authorized_user_info(info, scopes=[YOUTUBE_UPLOAD_SCOPE])
    except (ValueError, TypeError, KeyError):
        raise YouTubeCredentialsError(
            "Invalid YouTube credentials; reauthorize the channel"
        ) from None


def _serialize(credentials: Any) -> dict[str, Any]:
    """Never print Credentials, token JSON, or a provider error body."""
    try:
        data = json.loads(credentials.to_json())
        _check_stored_info(data)
        return data
    except (TypeError, ValueError, KeyError, AttributeError, YouTubeCredentialsError):
        raise YouTubeCredentialsError(
            "Incomplete YouTube credentials; reauthorize the channel"
        ) from None


def _request() -> Any:
    try:
        from google.auth.transport.requests import Request
    except ImportError:
        raise YouTubeConfigurationError(
            "Install optional YouTube dependencies: uv sync --locked --extra youtube"
        ) from None
    return Request()


def load_credentials(
    channel: str, store: YouTubeStore, *, request_factory: Callable[[], Any] = _request
) -> Any:
    """Load a channel's tokens and refresh if needed, never triggering a browser."""
    name = validate_channel_name(channel)
    store.ensure_directory()
    with channel_lock(store.directory, name):
        info = store.load_token(name)
        _check_stored_info(info)
        credentials = _google_credentials(info)
        if credentials.valid:
            return credentials
        if not credentials.refresh_token:
            raise YouTubeCredentialsError(
                "Missing YouTube refresh token; reauthorize the channel"
            )
        old_refresh_token = credentials.refresh_token
        try:
            credentials.refresh(request_factory())
        except Exception as exc:
            # The exception may contain token JSON or HTTP details. Never chain it.
            if type(exc).__name__ == "RefreshError":
                raise YouTubeAuthenticationError(
                    "YouTube authorization was rejected; reauthorize the channel"
                ) from None
            raise YouTubeAuthenticationError(
                "Could not refresh YouTube credentials; check the connection and retry"
            ) from None
        if not credentials.refresh_token:
            credentials.refresh_token = old_refresh_token
        if not credentials.valid:
            raise YouTubeAuthenticationError(
                "YouTube credentials are still invalid after refresh"
            )
        store.save_token(name, _serialize(credentials))
        return credentials


def authorize_channel(
    channel: str,
    store: YouTubeStore,
    *,
    reauthorize: bool = False,
    flow_factory: Callable[..., Any] | None = None,
) -> None:
    """Interactive, explicit authorization for exactly one local channel alias.

    OAuth cannot prove which channel was selected using only youtube.upload.
    A private test upload in the next phase verifies the actual destination.
    """
    name = validate_channel_name(channel)
    store.ensure_directory()
    with channel_lock(store.directory, name):
        destination = store.token_path(name)
        if destination.exists() and not reauthorize:
            raise YouTubeCredentialsError(
                "This channel already has credentials; use --reauthorize explicitly"
            )
        path = _oauth_client_file(store)
        if flow_factory is None:
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
            except ImportError:
                raise YouTubeConfigurationError(
                    "Install optional YouTube dependencies: uv sync --locked --extra youtube"
                ) from None
            flow_factory = InstalledAppFlow.from_client_secrets_file

        try:
            flow = flow_factory(
                str(path), scopes=[YOUTUBE_UPLOAD_SCOPE], autogenerate_code_verifier=True
            )
            credentials = flow.run_local_server(
                host="localhost",
                port=0,
                open_browser=True,
                authorization_prompt_message=None,
                success_message="Authorization received. You may close this browser window.",
                access_type="offline",
                prompt="consent",
            )
        except Exception:
            # Never echo original provider exceptions: they may contain OAuth code/state.
            raise YouTubeAuthenticationError(
                "YouTube authorization failed or was cancelled; credentials were not changed"
            ) from None
        if not credentials or not credentials.refresh_token:
            raise YouTubeAuthenticationError(
                "Google did not return a refresh token; credentials were not changed"
            )
        granted = getattr(credentials, "granted_scopes", None)
        if granted is not None and YOUTUBE_UPLOAD_SCOPE not in granted:
            raise YouTubeAuthenticationError(
                "Required YouTube upload permission was not granted"
            )
        data = _serialize(credentials)
        # Persist only after every OAuth check. On failed re-authorization the
        # previous file stays untouched, including its original refresh token.
        store.save_token(name, data)
