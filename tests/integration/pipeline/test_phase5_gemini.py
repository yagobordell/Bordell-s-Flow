"""Offline Phase 5 composition tests: Gemini must not touch GPU infrastructure."""

import argparse
import asyncio
import io
import json
import sys
import wave
from pathlib import Path

import pytest

from ai_video_factory.domain import SourceScript
from ai_video_factory.providers.speech import GeneratedSpeech
from scripts.pipeline import run_phase5_audio as phase5


def make_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24_000)
        wav_file.writeframes(bytes(2_400 * 2))
    return buffer.getvalue()


def _args(tmp_path: Path, *, provider: str = "gemini", preflight: bool = False):
    source = tmp_path / "source_script.json"
    source.write_text(SourceScript(text="A short, verified narration.").model_dump_json())
    folder = tmp_path / "phase5"
    return argparse.Namespace(
        source_file=source, provider=provider, preflight_only=preflight,
        gemini_model="gemini-3.8-flash-tts", gemini_voice="Algenib",
        primary_model="breeze-test", primary_voice="warm",
        fallback_model="fish-test", fallback_reason=None,
        cfg_scale=4.0, primary_seed=42, fallback_seed=42,
        poll_seconds=2.0, timeout_seconds=3.0, pending_timeout_seconds=3.0,
        allow_unconditioned_fish=True,
        output_dir=folder, metadata=folder / "narration.json",
        provenance=folder / "narration.provenance.json",
        fallback_state=folder / "fallback-state.json",
        instructions="Measured and professional.", speed=1.0,
    )


def _gpu_must_not_start(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Gemini attempted to construct Salad, R2 or Postgres")
    for name in ("create_r2_storage", "_executor", "_build_reference"):
        monkeypatch.setattr(phase5, name, unexpected)
    for name in ("SaladBreezeSpeechProvider", "SaladFishSpeechProvider"):
        monkeypatch.setattr(phase5, name, unexpected)


def test_gemini_preflight_only_validates_config_without_gpu_or_network(
    tmp_path, monkeypatch, capsys
):
    args = _args(tmp_path, preflight=True)
    monkeypatch.setattr(phase5, "parse_args", lambda: args)
    monkeypatch.setattr(phase5.settings, "gemini_api_key", "test-secret")
    _gpu_must_not_start(monkeypatch)

    async def network_must_not_run(*args, **kwargs):
        pytest.fail("preflight tried to generate audio")

    monkeypatch.setattr(phase5, "generate_narration_audio", network_must_not_run)
    asyncio.run(phase5.main())
    assert "PHASE5_PROVIDER_PREFLIGHT provider=gemini ok=true" in capsys.readouterr().out
    assert not args.output_dir.exists()


def test_gemini_preflight_requires_only_gemini_key(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path, preflight=True)
    monkeypatch.setattr(phase5, "parse_args", lambda: args)
    monkeypatch.setattr(phase5.settings, "gemini_api_key", None)
    _gpu_must_not_start(monkeypatch)
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        asyncio.run(phase5.main())


@pytest.mark.parametrize("field", ["gemini_model", "gemini_voice"])
def test_preflight_rejects_empty_model_or_voice(tmp_path, monkeypatch, field) -> None:
    args = _args(tmp_path, preflight=True)
    setattr(args, field, "   ")
    monkeypatch.setattr(phase5, "parse_args", lambda: args)
    monkeypatch.setattr(phase5.settings, "gemini_api_key", "test-secret")
    _gpu_must_not_start(monkeypatch)
    with pytest.raises(SystemExit, match="model and voice"):
        asyncio.run(phase5.main())


def test_gemini_generates_canonical_wav_and_does_not_clear_breeze_fallback(
    tmp_path, monkeypatch, capsys
) -> None:
    args = _args(tmp_path)
    monkeypatch.setattr(phase5, "parse_args", lambda: args)
    monkeypatch.setattr(phase5.settings, "gemini_api_key", "test-secret")
    _gpu_must_not_start(monkeypatch)
    args.output_dir.mkdir()
    args.fallback_state.write_text('{"pending": true}', encoding="utf-8")
    created = []

    class FakeGemini:
        def __init__(self, *, api_key):
            created.append(api_key)

        async def generate_speech(self, **kwargs):
            assert kwargs["text"] == "A short, verified narration."
            assert kwargs["model"] == args.gemini_model
            assert kwargs["voice"] == args.gemini_voice
            assert kwargs["instructions"] == args.instructions
            assert kwargs["output_format"] == "wav"
            return GeneratedSpeech(
                content=make_wav(), extension="wav", media_type="audio/wav",
                metadata={
                    "provider": "gemini",
                    "voice": args.gemini_voice,
                    "model": args.gemini_model,
                },
            )

    monkeypatch.setattr(phase5, "GeminiTtsSpeechProvider", FakeGemini)
    asyncio.run(phase5.main())
    assert created == ["test-secret"]
    assert (args.output_dir / "narration.wav").read_bytes() == make_wav()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    provenance = json.loads(args.provenance.read_text(encoding="utf-8"))
    assert metadata["duration_seconds"] == pytest.approx(0.1)
    assert provenance["provider"] == "gemini"
    assert provenance["voice"] == "Algenib"
    assert provenance["canonical_uri"] == "narration.wav"
    assert args.fallback_state.read_text(encoding="utf-8") == '{"pending": true}'
    output = capsys.readouterr().out
    assert "Generated with provider=gemini model=gemini-3.8-flash-tts" in output
    assert "Breeze generation profile" not in output


@pytest.mark.parametrize("provider", ["breeze", "fish"])
def test_existing_gpu_provider_preflight_still_constructs_infrastructure(
    tmp_path, monkeypatch, capsys, provider
) -> None:
    args = _args(tmp_path, provider=provider, preflight=True)
    monkeypatch.setattr(phase5, "parse_args", lambda: args)
    monkeypatch.setattr(phase5.settings, "gemini_api_key", None)
    for name in (
        "r2_endpoint_url", "r2_bucket", "r2_access_key_id", "r2_secret_access_key"
    ):
        monkeypatch.setattr(phase5.settings, name, "test")
    calls = []
    monkeypatch.setattr(
        phase5, "create_r2_storage",
        lambda **kwargs: calls.append(("r2", kwargs)) or object()
    )
    monkeypatch.setattr(
        phase5, "_executor",
        lambda **kwargs: calls.append(("postgres", kwargs)) or object()
    )
    monkeypatch.setattr(
        phase5, "SaladBreezeSpeechProvider",
        lambda **kwargs: calls.append(("breeze", kwargs)) or object()
    )
    monkeypatch.setattr(
        phase5, "SaladFishSpeechProvider",
        lambda **kwargs: calls.append(("fish", kwargs)) or object()
    )
    monkeypatch.setattr(phase5, "_build_reference", lambda args: None)
    asyncio.run(phase5.main())
    assert [entry[0] for entry in calls] == ["r2", "postgres", provider]
    assert f"PHASE5_PROVIDER_PREFLIGHT provider={provider} ok=true" in capsys.readouterr().out


def test_cli_selects_gemini_with_document_defaults(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_phase5_audio.py", "--provider", "gemini"])
    args = phase5.parse_args()
    assert args.provider == "gemini"
    assert args.gemini_model == "gemini-3.8-flash-tts"
    assert args.gemini_voice == "Algenib"
