"""Offline tests for persistent Segno QR codes, migration and exact URL decoding."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.providers.avatar_qr import ensure_avatar_qr, validate_ebook_url
from scripts.pipeline import run_b_pipeline as runner


def _avatar(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "monje.png"
    Image.new("RGB", (3, 2), "white").save(path, format="PNG")
    return path


def _decode_png(path: Path) -> str:
    """Use an independent QR reader in CI, not the Segno encoder under test."""
    decoder = shutil.which("zbarimg")
    if decoder is None:
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail("CI must install zbar-tools for independent QR decoding")
        pytest.skip("zbarimg not installed; independent decoder is required in CI")
    output = subprocess.run(
        [decoder, "--quiet", "--raw", str(path)],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    return output.stdout.removesuffix("\n")


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
def test_invalid_ebook_urls_fail_without_writing(url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        validate_ebook_url(url)


def test_generate_once_then_reuse_without_prompt_or_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    avatars = tmp_path / "avatar"
    avatar = _avatar(avatars)
    url = "https://ebooks.example/buy?product=23&utm=qr#checkout"
    result = ensure_avatar_qr(avatar_file=avatar, avatars_dir=avatars, input_url=url)
    slot = avatars / ".qr" / "monje.png"
    assert result.url == url
    assert result.generator == "segno"
    assert result.sha256 == hashlib.sha256((slot / "qr.png").read_bytes()).hexdigest()
    record = json.loads((slot / "qr.json").read_text(encoding="utf-8"))
    assert record["generator"] == "segno"
    assert record["url"] == url
    assert record["verification"] == "deterministic_qr_encoding"
    assert not (slot / "request_pending.json").exists()
    assert runner.available_avatars(avatars) == [avatar]

    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("Cached QR must not prompt"))
    monkeypatch.setattr(
        "ai_video_factory.providers.avatar_qr._generate_png",
        lambda _url: pytest.fail("Cached Segno QR must not be regenerated"),
    )
    assert ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=avatars, interactive=True
    ) == result


@pytest.mark.parametrize(
    "url",
    [
        "https://ebooks.example/monje",
        "https://ebooks.example/comprar?ref=video&producto=caf%C3%A9#pago",
    ],
)
def test_generated_png_decodes_to_exact_url(tmp_path: Path, url: str) -> None:
    avatar = _avatar(tmp_path / "avatar")
    ensure_avatar_qr(avatar_file=avatar, avatars_dir=avatar.parent, input_url=url)
    assert _decode_png(avatar.parent / ".qr" / avatar.name / "qr.png") == url


def test_first_time_prompts_only_for_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    avatar = _avatar(tmp_path / "avatar")
    answers = iter(["not a link", "https://ebooks.example/monje"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    result = ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=avatar.parent, interactive=True
    )
    assert result.url == "https://ebooks.example/monje"
    assert "URL" in capsys.readouterr().out


def test_missing_qr_noninteractive_requires_url(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path / "avatar")
    with pytest.raises(SystemExit, match="--qr-url URL"):
        ensure_avatar_qr(
            avatar_file=avatar, avatars_dir=avatar.parent, interactive=False
        )
    assert not (avatar.parent / ".qr").exists()


def test_saved_url_cannot_be_silently_reassigned(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path / "avatar")
    slot = avatar.parent / ".qr" / avatar.name
    original = ensure_avatar_qr(
        avatar_file=avatar,
        avatars_dir=avatar.parent,
        input_url="https://ebooks.example/first",
    )
    with pytest.raises(SystemExit, match="otro enlace"):
        ensure_avatar_qr(
            avatar_file=avatar,
            avatars_dir=avatar.parent,
            input_url="https://ebooks.example/second",
        )
    assert (slot / "qr.png").read_bytes() == original.png


def test_legacy_gpt_png_is_replaced_from_saved_url_without_prompt_or_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    avatar = _avatar(tmp_path / "avatar")
    slot = avatar.parent / ".qr" / avatar.name
    slot.mkdir(parents=True)
    old = b"GPT image that is not a QR"
    (slot / "qr.png").write_bytes(old)
    url = "https://ebooks.example/monje?utm=legacy"
    (slot / "qr.json").write_text(
        json.dumps(
            {
                "avatar_filename": avatar.name,
                "url": url,
                "sha256": hashlib.sha256(old).hexdigest(),
                "model": "gpt-6-luna",
                "image_model": "gpt-image-2.5-flare",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("Legacy URL should be reused")
    )
    result = ensure_avatar_qr(avatar_file=avatar, avatars_dir=avatar.parent)
    assert result.png != old
    assert result.url == url
    assert json.loads((slot / "qr.json").read_text(encoding="utf-8"))["generator"] == "segno"
    assert _decode_png(slot / "qr.png") == url


def test_old_paid_request_marker_is_resolved_locally(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path / "avatar")
    slot = avatar.parent / ".qr" / avatar.name
    slot.mkdir(parents=True)
    url = "https://ebooks.example/pendiente"
    (slot / "request_pending.json").write_text(
        json.dumps(
            {
                "avatar_filename": avatar.name,
                "url": url,
                "model": "gpt-6-luna",
                "status": "submitted_or_outcome_unknown",
            }
        ),
        encoding="utf-8",
    )
    result = ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=avatar.parent, interactive=False
    )
    assert result.url == url
    assert not (slot / "request_pending.json").exists()
    assert _decode_png(slot / "qr.png") == url


def test_corrupt_saved_segno_png_is_repaired_from_metadata(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path / "avatar")
    url = "https://ebooks.example/reparar"
    original = ensure_avatar_qr(
        avatar_file=avatar, avatars_dir=avatar.parent, input_url=url
    )
    slot = avatar.parent / ".qr" / avatar.name
    (slot / "qr.png").write_bytes(b"invalid image bytes")
    restored = ensure_avatar_qr(avatar_file=avatar, avatars_dir=avatar.parent)
    assert restored == original
    assert _decode_png(slot / "qr.png") == url


def test_missing_metadata_with_explicit_url_repairs_orphan_png(tmp_path: Path) -> None:
    avatar = _avatar(tmp_path / "avatar")
    slot = avatar.parent / ".qr" / avatar.name
    slot.mkdir(parents=True)
    (slot / "qr.png").write_bytes(b"orphan GPT image")
    result = ensure_avatar_qr(
        avatar_file=avatar,
        avatars_dir=avatar.parent,
        input_url="https://ebooks.example/recuperado",
    )
    assert result.png == (slot / "qr.png").read_bytes()
    assert json.loads((slot / "qr.json").read_text(encoding="utf-8"))["generator"] == "segno"


def test_qr_only_runs_without_guion_or_openai_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    avatar = _avatar(tmp_path / "avatar")
    args = Namespace(
        script_file=None,
        scripts_dir=tmp_path / "scripts",
        script=None,
        list_scripts=False,
        avatars_dir=avatar.parent,
        avatar=avatar.name,
        qr_url="https://ebooks.example/monje",
        qr_only=True,
        list_avatars=False,
    )
    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner.settings, "openai_api_key", None)
    monkeypatch.setattr(
        runner,
        "OpenAIProvider",
        lambda **_kwargs: pytest.fail("--qr-only must not create an OpenAI provider"),
    )
    asyncio.run(runner.main())
    assert (avatar.parent / ".qr" / avatar.name / "qr.png").is_file()
    assert "QR listo:" in capsys.readouterr().out


def test_cli_accepts_qr_only_without_script_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_b_pipeline.py",
            "--qr-only",
            "--avatar",
            "monje",
            "--qr-url",
            "https://ebooks.example/monje",
        ],
    )
    args = runner.parse_args()
    assert args.qr_only
    assert args.avatar == "monje"
    assert args.script is None
