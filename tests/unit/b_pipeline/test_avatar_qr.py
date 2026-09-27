"""Offline tests for first-use QR generation, caching, and paid-call safeguards."""

import asyncio
import base64
import hashlib
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.providers.avatar_qr import (
    QR_SYSTEM_PROMPT,
    ensure_avatar_qr,
    validate_ebook_url,
)
from scripts.pipeline.run_b_pipeline import available_avatars


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (512, 512), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _avatar(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "monje.png"
    Image.new("RGB", (3, 2), "white").save(path, format="PNG")
    return path


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://example.com/ebook",
        "https://",
        "https://user:pass@example.com/ebook",
        "https://example.com/a b",
        "https://example.com/ebook\n",
        "https://example.com:wrong/ebook",
    ],
)
def test_invalid_ebook_urls_fail_before_any_api_call(url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        validate_ebook_url(url)


def test_generate_once_then_reuse_without_user_input_or_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    avatars_dir = tmp_path / "avatar"
    avatar = _avatar(avatars_dir)
    expected_url = "https://ebooks.example/buy?product=23&utm=qr#checkout"
    generated_png = _png()
    submitted: list[dict[str, object]] = []

    async def create(**kwargs):
        submitted.append(kwargs)
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="image_generation_call",
                    result=base64.b64encode(generated_png).decode("ascii"),
                )
            ]
        )

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    options = {
        "avatar_file": avatar,
        "avatars_dir": avatars_dir,
        "api_key": "fake-key",
        "model": "gpt-6-luna",
        "image_model": "gpt-image-2.5-flare",
        "client": client,
    }
    result = asyncio.run(ensure_avatar_qr(**options, input_url=expected_url))
    assert submitted == [
        {
            "model": "gpt-6-luna",
            "instructions": QR_SYSTEM_PROMPT,
            "input": expected_url,
            "tools": [{"type": "image_generation", "model": "gpt-image-2.5-flare"}],
            "tool_choice": {"type": "image_generation"},
        }
    ]
    slot = avatars_dir / ".qr" / "monje.png"
    assert (slot / "qr.png").read_bytes() == generated_png
    assert result.sha256 == hashlib.sha256(generated_png).hexdigest()
    metadata = json.loads((slot / "qr.json").read_text(encoding="utf-8"))
    assert metadata["url"] == expected_url
    assert metadata["verification"] == "png_validated_scan_pending"
    assert not (slot / "request_pending.json").exists()
    assert available_avatars(avatars_dir) == [avatar]

    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("The cached QR must not prompt")
    )
    reused = asyncio.run(ensure_avatar_qr(**options, interactive=True))
    assert reused == result
    assert len(submitted) == 1


def test_new_avatar_prompts_for_the_ebook_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    avatar = _avatar(tmp_path / "avatar")
    answers = iter(["not a link", "https://ebooks.example/monje"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    submitted = []

    async def create(**kwargs):
        submitted.append(kwargs)
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="image_generation_call",
                    result=base64.b64encode(_png()).decode("ascii"),
                )
            ]
        )

    result = asyncio.run(
        ensure_avatar_qr(
            avatar_file=avatar,
            avatars_dir=avatar.parent,
            api_key="fake-key",
            model="gpt-6-luna",
            image_model="gpt-image-2.5-flare",
            interactive=True,
            client=SimpleNamespace(responses=SimpleNamespace(create=create)),
        )
    )
    assert result.url == "https://ebooks.example/monje"
    assert len(submitted) == 1


def test_missing_qr_noninteractive_fails_without_paid_call(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path / "avatar")
    with pytest.raises(SystemExit, match="--qr-url URL"):
        asyncio.run(
            ensure_avatar_qr(
                avatar_file=avatar,
                avatars_dir=avatar.parent,
                api_key="fake-key",
                model="gpt-6-luna",
                image_model="gpt-image-2.5-flare",
                interactive=False,
            )
        )
    assert not (avatar.parent / ".qr").exists()


def test_cached_url_cannot_be_silently_reassigned(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path / "avatar")
    slot = avatar.parent / ".qr" / avatar.name
    slot.mkdir(parents=True)
    png = _png()
    (slot / "qr.png").write_bytes(png)
    (slot / "qr.json").write_text(
        json.dumps(
            {
                "avatar_filename": avatar.name,
                "url": "https://ebooks.example/first",
                "sha256": hashlib.sha256(png).hexdigest(),
                "model": "gpt-6-luna",
                "image_model": "gpt-image-2.5-flare",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="otro enlace"):
        asyncio.run(
            ensure_avatar_qr(
                avatar_file=avatar,
                avatars_dir=avatar.parent,
                api_key="fake-key",
                model="gpt-6-luna",
                image_model="gpt-image-2.5-flare",
                input_url="https://ebooks.example/second",
            )
        )
    assert (slot / "qr.png").read_bytes() == png


def test_timeout_creates_pending_marker_and_prevents_duplicate_paid_calls(
    tmp_path: Path,
) -> None:
    avatar = _avatar(tmp_path / "avatar")
    attempts = []

    async def failed_create(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("outcome unknown")

    opts = {
        "avatar_file": avatar,
        "avatars_dir": avatar.parent,
        "api_key": "fake-key",
        "model": "gpt-6-luna",
        "image_model": "gpt-image-2.5-flare",
        "input_url": "https://ebooks.example/monje",
        "client": SimpleNamespace(responses=SimpleNamespace(create=failed_create)),
    }
    with pytest.raises(SystemExit, match="Revisa"):
        asyncio.run(ensure_avatar_qr(**opts))
    assert len(attempts) == 1
    assert (avatar.parent / ".qr" / avatar.name / "request_pending.json").is_file()
    with pytest.raises(SystemExit, match="pendiente"):
        asyncio.run(ensure_avatar_qr(**opts))
    assert len(attempts) == 1


def test_corrupt_saved_qr_fails_closed_and_preserves_previous_artifacts(
    tmp_path: Path,
) -> None:
    avatar = _avatar(tmp_path / "avatar")
    slot = avatar.parent / ".qr" / avatar.name
    slot.mkdir(parents=True)
    (slot / "qr.png").write_bytes(b"not a PNG")
    (slot / "qr.json").write_text(
        json.dumps(
            {
                "avatar_filename": avatar.name,
                "url": "https://ebooks.example/monje",
                "sha256": hashlib.sha256(b"not a PNG").hexdigest(),
                "model": "gpt-6-luna",
                "image_model": "gpt-image-2.5-flare",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="QR guardado inválido"):
        asyncio.run(
            ensure_avatar_qr(
                avatar_file=avatar,
                avatars_dir=avatar.parent,
                api_key="fake-key",
                model="gpt-6-luna",
                image_model="gpt-image-2.5-flare",
            )
        )
    assert (slot / "qr.png").read_bytes() == b"not a PNG"
