"""Google dependencies are lazily imported only when a client is requested."""

import sys
from types import ModuleType
from unittest.mock import Mock, patch

import pytest

from ai_video_factory.integrations.youtube.client import build_youtube_client
from ai_video_factory.integrations.youtube.errors import YouTubeCredentialsError


def test_missing_credentials_rejected_before_import() -> None:
    with pytest.raises(YouTubeCredentialsError):
        build_youtube_client(None)


def test_youtube_client_uses_minimal_v3_factory() -> None:
    package = ModuleType("googleapiclient")
    package.__path__ = []  # type: ignore[attr-defined]
    discovery = ModuleType("googleapiclient.discovery")
    builder = Mock(return_value=object())
    discovery.build = builder  # type: ignore[attr-defined]
    modules = {"googleapiclient": package, "googleapiclient.discovery": discovery}
    with patch.dict(sys.modules, modules):
        credentials = object()
        client = build_youtube_client(credentials)
    assert client is builder.return_value
    builder.assert_called_once_with(
        "youtube", "v3", credentials=credentials, cache_discovery=False,
    )
