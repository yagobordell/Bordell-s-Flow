"""Offline contract tests for the direct Gemini 3.8 TTS adapter."""

import asyncio
import base64
import io
import json
import wave
from urllib.error import HTTPError, URLError

import pytest

from ai_video_factory.providers import GeminiTtsSpeechProvider
from ai_video_factory.providers import gemini_tts as gemini


def make_wav(
    *, sample_rate: int = 24_000, channels: int = 1,
    sample_width: int = 2, frames: int = 2_400
) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00" * frames * channels * sample_width)
    return buffer.getvalue()


def answer(audio: bytes, mime: str = "audio/wav") -> dict:
    return {
        "candidates": [
            {"content": {"parts": [
                {"text": "not the audio"},
                {"inlineData": {"mimeType": mime, "data": base64.b64encode(audio).decode()}}
            ]}}
        ]
    }


def generate(provider, **overrides):
    params = {
        "text": "Hello, café.",
        "model": gemini.DEFAULT_GEMINI_TTS_MODEL,
        "voice": gemini.DEFAULT_GEMINI_TTS_VOICE,
        "instructions": "Soft documentary voice.",
        "speed": 1.0,
        "output_format": "wav",
    }
    params.update(overrides)
    return asyncio.run(provider.generate_speech(**params))


def test_payload_voice_model_style_and_wav_contract(monkeypatch) -> None:
    calls = []

    def fake_post(**kwargs):
        calls.append(kwargs)
        return answer(make_wav())

    monkeypatch.setattr(gemini, "_post_json", fake_post)
    result = generate(GeminiTtsSpeechProvider(api_key="test-secret"), model="custom-model")
    assert result.content == make_wav()
    assert result.extension == "wav"
    assert result.media_type == "audio/wav"
    assert result.metadata == {
        "provider": "gemini",
        "model": "custom-model",
        "voice": "Algenib",
        "sample_rate_hz": 24000,
        "channels": 1,
    }
    assert len(calls) == 1
    request = calls[0]
    assert request["url"].endswith("/models/custom-model:generateContent")
    assert request["api_key"] == "test-secret"
    config = request["payload"]["generationConfig"]
    assert config["responseModalities"] == ["AUDIO"]
    assert config["responseFormat"]["audio"] == {
        "mimeType": "AUDIO_WAV", "sampleRate": 24000
    }
    assert config["speechConfig"]["voiceConfig"]["voice"] == "Algenib"
    part = request["payload"]["contents"][0]["parts"][0]
    assert part["text"] == "Hello, café."
    assert "Soft documentary voice." in part["speech_metadata"]["style"]
    assert "verbatim transcript" in part["speech_metadata"]["style"]
    assert "Hello, café." not in part["speech_metadata"]["style"]


@pytest.mark.parametrize(
    ("speed", "expected"),
    [
        (0.8, "measured, slightly slower"),
        (1.0, "natural conversational"),
        (1.2, "brisk pace"),
    ],
)
def test_speed_becomes_style_hint_not_transcript(speed, expected) -> None:
    payload = gemini._build_payload(
        text="Spoken words.", voice="Algenib", instructions="Calm.", speed=speed
    )
    part = payload["contents"][0]["parts"][0]
    assert part["text"] == "Spoken words."
    assert expected in part["speech_metadata"]["style"]


def test_utf8_style_limit_preserves_mandatory_instructions() -> None:
    style = gemini._build_style("á" * 6_000, 1.0)
    assert len(style.encode("utf-8")) <= gemini.MAX_STYLE_PROMPT_BYTES
    assert style.startswith("á")
    assert "Do not add, omit, or rewrite words." in style
    assert "natural conversational pace" in style


@pytest.mark.parametrize("speed", [float("nan"), float("inf"), 0.0, 4.1])
def test_invalid_speed_fails_before_network(speed, monkeypatch) -> None:
    monkeypatch.setattr(
        gemini, "_post_json", lambda **kwargs: pytest.fail("unexpected HTTP request")
    )
    with pytest.raises(ValueError, match="speed"):
        generate(GeminiTtsSpeechProvider(api_key="test"), speed=speed)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"text": "  "}, "text"),
        ({"model": ""}, "model"),
        ({"voice": ""}, "voice"),
        ({"output_format": "mp3"}, "WAV"),
    ],
)
def test_invalid_request_never_submits(monkeypatch, overrides, message) -> None:
    monkeypatch.setattr(
        gemini, "_post_json", lambda **kwargs: pytest.fail("unexpected HTTP request")
    )
    with pytest.raises(ValueError, match=message):
        generate(GeminiTtsSpeechProvider(api_key="test"), **overrides)


@pytest.mark.parametrize(
    "key",
    ["", "   "],
)
def test_empty_api_key_fails_immediately(key) -> None:
    with pytest.raises(ValueError, match="API key"):
        GeminiTtsSpeechProvider(api_key=key)


def test_request_sends_key_only_in_header_and_valid_json(monkeypatch) -> None:
    received = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return b'{"candidates": []}'

    def fake_urlopen(request, *, timeout):
        received.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(gemini, "urlopen", fake_urlopen)
    result = gemini._post_json(
        url="https://example.test/v1beta/models/tts:generateContent",
        api_key="private-key",
        payload={"text": "café"},
        timeout_seconds=13.0,
    )
    assert result == {"candidates": []}
    assert len(received) == 1
    req, timeout = received[0]
    assert req.get_method() == "POST"
    assert timeout == 13.0
    assert req.get_header("X-goog-api-key") == "private-key"
    assert req.get_header("Content-type") == "application/json"
    assert b"private-key" not in req.data
    assert "private-key" not in req.full_url
    assert json.loads(req.data)["text"] == "café"


@pytest.mark.parametrize("code", [400, 401, 429, 500, 503])
def test_http_error_does_not_echo_key_or_retry(monkeypatch, code) -> None:
    calls = []
    secret = "very-private-key"

    def fail_once(request, *, timeout):
        calls.append(request)
        raise HTTPError(
            request.full_url, code, f"error mentions {secret}", {},
            io.BytesIO(f"debug: {secret}".encode())
        )

    monkeypatch.setattr(gemini, "urlopen", fail_once)
    with pytest.raises(gemini.GeminiTtsError, match=f"HTTP {code}") as caught:
        gemini._post_json(
            url="https://example.test/generate",
            api_key=secret,
            payload={},
            timeout_seconds=3.0,
        )
    assert secret not in str(caught.value)
    assert len(calls) == 1


def test_transport_error_does_not_retry_or_reveal_key(monkeypatch) -> None:
    calls = []

    def fail(request, *, timeout):
        calls.append(request)
        raise URLError("connection timed out")

    monkeypatch.setattr(gemini, "urlopen", fail)
    with pytest.raises(gemini.GeminiTtsError, match="outcome may be unknown"):
        gemini._post_json(
            url="https://example.test/generate",
            api_key="secret",
            payload={},
            timeout_seconds=3.0,
        )
    assert len(calls) == 1


@pytest.mark.parametrize("bad", [b"invalid", b"[]", b'{"valid":', b"null"])
def test_invalid_json_or_nonobject_fails(monkeypatch, bad) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return bad

    monkeypatch.setattr(gemini, "urlopen", lambda *a, **k: Response())
    with pytest.raises(gemini.GeminiTtsError, match="JSON"):
        gemini._post_json(
            url="https://example.test/generate",
            api_key="secret",
            payload={},
            timeout_seconds=1.0,
        )


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({}, "no candidates"),
        ({"candidates": [{"content": {"parts": [{"text": "plain"}]}}]}, "no inline audio"),
        (answer(make_wav(), "audio/mpeg"), "unexpected MIME"),
        (answer(make_wav(), ""), "unexpected MIME"),
        ({"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "audio/wav", "data": "%%%"}}
        ]}}]}, "base64"),
        ({"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "audio/wav", "data": ""}}
        ]}}]}, "empty audio"),
    ],
)
def test_decode_rejects_invalid_audio(response, message) -> None:
    with pytest.raises(gemini.GeminiTtsError, match=message):
        gemini._decode_audio(response)


@pytest.mark.parametrize(
    ("wav_bytes", "message"),
    [
        (b"not a wave", "invalid WAV"),
        (make_wav(sample_rate=16000), "expected 24000"),
        (make_wav(channels=2), "expected mono"),
        (make_wav(sample_width=1), "PCM16"),
        (make_wav(frames=0), "invalid WAV|empty WAV"),
        (make_wav()[:-30], "truncated WAV|incomplete WAV"),
    ],
    ids=[
        "not-wav", "wrong-sample-rate", "stereo-not-mono",
        "not-pcm16", "empty-wav", "truncated-wav",
    ],
)
def test_rejects_invalid_wav(wav_bytes, message) -> None:
    with pytest.raises(gemini.GeminiTtsError, match=message):
        gemini._validate_wav(wav_bytes)


def test_valid_wav_accepts_x_wav_with_mime_parameters() -> None:
    wav = make_wav()
    result = gemini._decode_audio(answer(wav, "audio/x-wav; rate=24000"))
    assert result == wav
    assert gemini._validate_wav(result) == {"sample_rate_hz": 24000, "channels": 1}


def test_async_provider_yields_to_event_loop(monkeypatch) -> None:
    monkeypatch.setattr(gemini, "_post_json", lambda **kwargs: answer(make_wav()))
    async def run_both():
        provider = GeminiTtsSpeechProvider(api_key="test")
        task = asyncio.create_task(provider.generate_speech(
            text="Hello.", model="gemini-3.8-flash-tts", voice="Algenib",
            instructions="", speed=1.0, output_format="wav"
        ))
        await asyncio.sleep(0)
        return await task
    assert asyncio.run(run_both()).extension == "wav"


def test_voice_display_name_resolves_to_voice_library_id(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "voices": [{
                    "id": "voice_0da4kq24ctjr",
                    "display_name": "Uncle Enzo 3",
                }],
            }).encode()

    requests = []

    def fake_get(request, *, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(gemini, "urlopen", fake_get)
    assert gemini.resolve_gemini_voice_id(
        "Uncle Enzo 3", api_key="test-key"
    ) == "voice_0da4kq24ctjr"
    assert requests[0][0].full_url.endswith("/voices?pageSize=1000")
    assert requests[0][0].get_header("X-goog-api-key") == "test-key"


def test_custom_studio_voice_uses_exported_name_not_legacy_id(monkeypatch) -> None:
    """Google Studio's `Uncle Enzo 3` is a name with spaces, not voice_... ID."""
    requests = []

    def fake_post(**kwargs):
        requests.append(kwargs)
        return answer(make_wav())

    monkeypatch.setattr(gemini, "_post_json", fake_post)
    provider = GeminiTtsSpeechProvider(api_key="test")
    generated = generate(provider, voice="Uncle Enzo 3")
    assert generated.metadata["voice"] == "Uncle Enzo 3"
    config = requests[0]["payload"]["generationConfig"]
    assert config == {
        "responseModalities": ["AUDIO"],
        "speechConfig": {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Uncle Enzo 3"}}
        },
    }
    # The Phase 5 style and text remain separated.
    part = requests[0]["payload"]["contents"][0]["parts"][0]
    assert part["text"] == "Hello, café."
    assert "Soft documentary voice." in part["speech_metadata"]["style"]


def test_custom_voice_in_professional_v3_preserves_individual_part_styles() -> None:
    turns = [
        {"text": "Hello <breath>there.", "style": "Measured opening."},
        {"text": "Next.", "style": "Accelerating."},
    ]
    payload = gemini._build_directed_payload(turns=turns, voice="Uncle Enzo 3")
    assert payload["generationConfig"] == {
        "responseModalities": ["AUDIO"],
        "speechConfig": {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Uncle Enzo 3"}}
        },
    }
    parts = payload["contents"][0]["parts"]
    assert [part["text"] for part in parts] == ["Hello <breath>there.", "Next."]
    assert "Measured opening." in parts[0]["speech_metadata"]["style"]
    assert "Accelerating." in parts[1]["speech_metadata"]["style"]
    assert all("verbatim transcript" in part["speech_metadata"]["style"] for part in parts)


def test_studio_pcm16_is_converted_to_valid_canonical_wav(monkeypatch) -> None:
    # The Studio export may return audio/L16 instead of an AUDIO_WAV container.
    raw_pcm = b"\x00\x00\x20\x00" * 1200
    sent = []

    def fake_post(**kwargs):
        sent.append(kwargs)
        return answer(raw_pcm, "audio/L16;rate=24000")

    monkeypatch.setattr(gemini, "_post_json", fake_post)
    output = generate(GeminiTtsSpeechProvider(api_key="test"), voice="Uncle Enzo 3")
    with wave.open(io.BytesIO(output.content), "rb") as wav_file:
        assert wav_file.getframerate() == 24_000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.readframes(2400) == raw_pcm
    assert sent[0]["payload"]["generationConfig"]["speechConfig"]["voiceConfig"] == {
        "prebuiltVoiceConfig": {"voiceName": "Uncle Enzo 3"}
    }


@pytest.mark.parametrize(
    "mime",
    ["audio/L16;rate=16000", "audio/L16;rate=24000;channels=2", "audio/L16"],
)
def test_studio_pcm_wrong_format_is_rejected(mime) -> None:
    with pytest.raises(gemini.GeminiTtsError, match="unsupported PCM"):
        gemini._decode_audio(answer(b"\x00\x00" * 120, mime))


def test_studio_pcm_with_odd_sample_bytes_is_rejected() -> None:
    with pytest.raises(gemini.GeminiTtsError, match="incomplete PCM16"):
        gemini._decode_audio(answer(b"\x00\x00\x01", "audio/L16;rate=24000"))


@pytest.mark.parametrize(
    "name",
    ["Uncle Enzo 3\n", " Uncle Enzo 3", "Uncle  Enzo 3", "Uncle/Enzo", "3;", "x"],
)
def test_invalid_custom_voice_never_posts(name, monkeypatch) -> None:
    monkeypatch.setattr(
        gemini, "_post_json", lambda **kwargs: pytest.fail("Invalid voice submitted")
    )
    with pytest.raises(ValueError, match="voice"):
        generate(GeminiTtsSpeechProvider(api_key="test"), voice=name)


def test_legacy_voice_id_stays_on_existing_wire_contract() -> None:
    for name in ("Algenib", "Kore", "voice_abcd123"):
        config = gemini._build_payload(
            text="Hi", voice=name, instructions="Clear", speed=1.0,
        )["generationConfig"]
        assert config["speechConfig"]["voiceConfig"] == {"voice": name}
        assert config["responseFormat"]["audio"]["mimeType"] == "AUDIO_WAV"
