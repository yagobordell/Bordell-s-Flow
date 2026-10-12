"""Verify Fish starts after B1.1 beside B1.2 and audio-only avoids B2/images."""

import asyncio
import hashlib
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.bots.contracts import B11Output, WholeB12Output
from scripts.pipeline import run_b_pipeline as runner

VOICE = "fishaudio_80e34d5e0b2b4577a486f3a77e357261"


def _fake_avatar_qr(*, avatar_file: Path, **_kwargs):
    data = avatar_file.read_bytes()
    return SimpleNamespace(
        url="https://ebooks.example/avatar",
        png=data,
        sha256=hashlib.sha256(data).hexdigest(),
        generator="segno",
    )



def _fake_ebook_covers(avatar_file: Path) -> list[str]:
    """Keep legacy Casero/Fish tests independent of real ebook onboarding."""
    meta = avatar_file.parent / "meta"
    meta.mkdir(exist_ok=True)
    (meta / "qr.png").write_bytes(avatar_file.read_bytes())
    return ["cover"]

def _files(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "input" / "test2.txt"
    script.parent.mkdir()
    script.write_bytes(b"  Uno.\r\nDos.  ")
    avatar = tmp_path / "avatar" / "Jorge.png"
    avatar.parent.mkdir()
    Image.new("RGB", (2, 2)).save(avatar, format="PNG")
    return script, avatar


def _b11_payload() -> dict:
    return {
        "pipeline_stage": "B1.1",
        "narrative_core": {"central_question": "Pregunta", "final_answer": "Respuesta"},
        "blocks": [{
            "block_id": 1, "type": "intro", "emotional_entry": "calm",
            "emotional_exit": "curious",
            "span": {"first_words": "Uno.", "last_words": "Dos."},
        }],
        "error": None,
    }


@pytest.mark.parametrize("no_audio", [False, True])
def test_full_b_run_starts_fish_concurrently_and_no_audio_skips_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_audio: bool
) -> None:
    script, avatar = _files(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    created_audio = []

    args = Namespace(
        visual_style="casero",
        script_file=None,
        scripts_dir=script.parent,
        script=script.name,
        list_scripts=False,
        avatars_dir=avatar.parent,
        avatar=avatar.name,
        list_avatars=False,
        output=tmp_path / "output",
        max_parallel_calls=1,
        from_stage="B1.1",
        skip_images=True,
        no_audio=no_audio,
        images_only=False,
        regenerate_audio=False,
        resume_audio=False,
    )

    async def fake_audio(
        source, destination, *, provider=None, b11=None, max_parallel_calls=8, resume=False
    ):
        created_audio.append((source, destination, provider, resume))
        started.set()
        await release.wait()
        return {
            "status": "completed",
            "voice_id": VOICE,
            "task_id": "fake-audio-task",
            "file": "audio/runs/example/audio.mp3",
        }

    async def fake_b(source, **kwargs):
        assert source == script.read_bytes().decode("utf-8")
        kwargs["on_output"]("B1.1", None, _b11_payload())
        if not no_audio:
            await asyncio.wait_for(started.wait(), 2)
        else:
            assert not created_audio
        release.set()
        return SimpleNamespace(
            b12=[SimpleNamespace(block_id=1)],
            visual_plan=lambda: {"blocks": []},
        )

    def prohibit_images(*_args, **_kwargs):
        raise AssertionError("--no-image must not submit images")

    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner, "ensure_avatar_qr", _fake_avatar_qr)
    monkeypatch.setattr(
        runner, "ensure_ebook_covers",
        lambda avatar_file, **_kw: _fake_ebook_covers(avatar_file),
    )
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-openai-key")
    monkeypatch.setattr(runner.settings, "ai33_api_key", "test-ai33-key")
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **_kw: object())
    monkeypatch.setattr(runner, "run_b_pipeline", fake_b)
    monkeypatch.setattr(runner, "_generate_audio", fake_audio)
    monkeypatch.setattr(runner, "_generate_images", prohibit_images)
    asyncio.run(runner.main())

    report = json.loads(
        (tmp_path / "output/test2/run_report.json").read_text(encoding="utf-8")
    )
    assert report["run"]["status"] == "completed"
    if no_audio:
        assert report["run"]["audio_generation"] == {"status": "skipped"}
        assert not created_audio
        assert not (tmp_path / "output/test2/audio").exists()
    else:
        assert len(created_audio) == 1
        assert created_audio[0][0] == script.read_bytes().decode("utf-8")
        assert report["run"]["audio_generation"]["task_id"] == "fake-audio-task"
    assert (tmp_path / "output/test2/visual_plan.json").is_file()


@pytest.mark.parametrize("resume", [False, True])
def test_audio_only_mode_preserves_b_artifacts_and_skips_avatar_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    script, avatar = _files(tmp_path)
    root = tmp_path / "output"
    folder = root / "test2"
    (folder / "B2").mkdir(parents=True)
    (folder / "B1.1").mkdir(parents=True)
    (folder / "B1.1/output.json").write_text(
        json.dumps(_b11_payload()), encoding="utf-8"
    )
    (folder / "B2" / "merged_output.json").write_text("{}", encoding="utf-8")
    (folder / "visual_plan.json").write_text("{}", encoding="utf-8")
    report = {
        "run": {
            "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "status": "completed",
        },
        "timings": {"status": "completed"},
        "api_costs": {"run_status": "completed"},
    }
    (folder / "run_report.json").write_text(json.dumps(report), encoding="utf-8")
    pristine_b2 = (folder / "B2" / "merged_output.json").read_bytes()
    pristine_plan = (folder / "visual_plan.json").read_bytes()
    args = Namespace(
        script_file=None,
        scripts_dir=script.parent,
        script=script.name,
        list_scripts=False,
        avatars_dir=avatar.parent,
        avatar=None,
        list_avatars=False,
        output=root,
        max_parallel_calls=1,
        from_stage="B1.1",
        skip_images=False,
        no_audio=False,
        images_only=False,
        regenerate_audio=not resume,
        resume_audio=resume,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Audio-only mode must not touch B, avatar or images")

    calls = []

    async def fake_audio(
        source, destination, *, provider=None, b11=None, max_parallel_calls=8, resume=False
    ):
        calls.append((source, destination, provider, resume))
        return {"status": "completed", "file": "audio/new.mp3", "voice_id": VOICE}

    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner, "ensure_avatar_qr", _fake_avatar_qr)
    monkeypatch.setattr(
        runner, "ensure_ebook_covers",
        lambda avatar_file, **_kw: _fake_ebook_covers(avatar_file),
    )
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-openai-key")
    monkeypatch.setattr(runner.settings, "ai33_api_key", "test-ai33-key")
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **_kw: object())
    monkeypatch.setattr(runner, "run_b_pipeline", forbidden)
    monkeypatch.setattr(runner, "_generate_images", forbidden)
    monkeypatch.setattr(runner, "select_avatar", forbidden)
    monkeypatch.setattr(runner, "_generate_audio", fake_audio)
    asyncio.run(runner.main())
    assert len(calls) == 1
    assert calls[0][3] is resume
    assert (calls[0][2] is None) is resume
    assert (folder / "B2" / "merged_output.json").read_bytes() == pristine_b2
    assert (folder / "visual_plan.json").read_bytes() == pristine_plan
    persisted = json.loads((folder / "run_report.json").read_text(encoding="utf-8"))
    assert persisted["run"]["status"] == "completed"
    assert persisted["run"]["audio_generation"]["file"] == "audio/new.mp3"
    assert not (folder / "images").exists()


def test_audio_only_rejects_modified_source_before_new_paid_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, _ = _files(tmp_path)
    folder = tmp_path / "output/test2"
    folder.mkdir(parents=True)
    (folder / "run_report.json").write_text(
        json.dumps({"run": {"script_sha256": hashlib.sha256(b"old").hexdigest()}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runner.settings, "ai33_api_key", "test-key")
    monkeypatch.setattr(
        runner, "_generate_audio",
        lambda *_args, **_kwargs: pytest.fail("Audio must not be charged"),
    )
    with pytest.raises(SystemExit, match="Script changed"):
        asyncio.run(runner._run_audio_only(script, tmp_path / "output", resume=False))


@pytest.mark.parametrize("alignment_ok", [True, False])
def test_images_wait_for_b2_and_validated_stt_before_paid_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alignment_ok: bool
) -> None:
    script, avatar = _files(tmp_path)
    audio_started = asyncio.Event()
    audio_released = asyncio.Event()
    order = []
    args = Namespace(
        visual_style="casero",
        script_file=None,
        scripts_dir=script.parent,
        script=script.name,
        list_scripts=False,
        avatars_dir=avatar.parent,
        avatar=avatar.name,
        list_avatars=False,
        output=tmp_path / "output",
        max_parallel_calls=1,
        from_stage="B1.1",
        skip_images=False,
        no_audio=False,
        images_only=False,
        regenerate_audio=False,
        resume_audio=False,
    )

    async def fake_audio(
        _source, _output, *, provider=None, b11=None, max_parallel_calls=8, resume=False
    ):
        order.append("audio_started")
        audio_started.set()
        await audio_released.wait()
        order.append("audio_completed")
        return {
            "status": "completed",
            "stt_complete": True,
            "voice_id": VOICE,
            "file": "audio/ready.mp3",
        }

    async def fake_b(_source, **kwargs):
        kwargs["on_output"]("B1.1", None, _b11_payload())
        await asyncio.wait_for(audio_started.wait(), 2)
        order.append("b2_completed")
        audio_released.set()
        return SimpleNamespace(
            b12=[SimpleNamespace(block_id=1)],
            visual_plan=lambda: {"blocks": []},
        )

    async def fake_alignment(destination: Path, _plan: dict, audio: dict):
        assert destination == tmp_path / "output/test2"
        assert audio["status"] == "completed"
        order.append("stt_checked")
        if not alignment_ok:
            raise runner.SttAlignmentError(1, "STT mismatch")
        return {"status": "completed"}

    async def fake_images(destination: Path, _plan: dict):
        assert (destination / "visual_plan.json").is_file()
        assert order == [
            "audio_started", "b2_completed", "audio_completed", "stt_checked",
        ]
        order.append("images_started")
        return {"model_id": "gpt-image-2.5-flare", "items": []}

    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner, "ensure_avatar_qr", _fake_avatar_qr)
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-openai-key")
    monkeypatch.setattr(runner.settings, "ai33_api_key", "test-ai33-key")
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **_kw: object())
    monkeypatch.setattr(runner, "run_b_pipeline", fake_b)
    monkeypatch.setattr(runner, "_generate_audio", fake_audio)
    monkeypatch.setattr(
        runner, "ensure_ebook_covers",
        lambda avatar_file, **_kw: _fake_ebook_covers(avatar_file),
    )
    monkeypatch.setattr(runner, "_preflight_stt_alignment_with_retries", fake_alignment)
    monkeypatch.setattr(runner, "_generate_images", fake_images)
    async def fake_cta(**_kw):
        return {"directory": str(tmp_path / "cta"), "status": "completed"}

    monkeypatch.setattr(runner, "prepare_casero_cta", fake_cta)

    async def run_bounded():
        await asyncio.wait_for(runner.main(), timeout=5)

    if alignment_ok:
        asyncio.run(run_bounded())
        assert order == [
            "audio_started", "b2_completed", "audio_completed", "stt_checked",
            "images_started",
        ]
    else:
        with pytest.raises(runner.SttAlignmentError, match="STT mismatch"):
            asyncio.run(run_bounded())
        assert order == [
            "audio_started", "b2_completed", "audio_completed", "stt_checked",
        ]
        report = json.loads(
            (tmp_path / "output/test2/run_report.json").read_text(encoding="utf-8")
        )
        assert report["run"]["stt_alignment"]["status"] == "failed"



def test_fish_director_uses_shared_text_model_without_external_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.bots.contracts import B11Output

    captured: dict[str, object] = {}
    provider = object()
    stt_client = object()

    async def fake_generate_fish_audio(
        source: str, output: Path, **kwargs: object,
    ) -> dict[str, object]:
        captured.update(kwargs)
        assert source == "Uno. Dos."
        assert output == tmp_path
        return {"status": "completed"}

    monkeypatch.setattr(runner, "generate_fish_audio", fake_generate_fish_audio)
    monkeypatch.setattr(runner, "OpenAISttClient", lambda *, api_key: stt_client)
    monkeypatch.setattr(runner.settings, "openai_model", "gpt-6-luna")
    monkeypatch.setattr(runner.settings, "openai_reasoning_effort", "medium")
    monkeypatch.setattr(runner.settings, "openai_api_key", "fake-key")
    b11 = B11Output.model_validate(_b11_payload())
    outcome = asyncio.run(
        runner._generate_audio("Uno. Dos.", tmp_path, provider=provider, b11=b11)
    )

    assert outcome == {"status": "completed"}
    assert captured["director_model"] == runner.settings.openai_model
    assert captured["provider"] is provider
    assert captured["stt_client"] is stt_client


def test_stop_saves_resumable_stages_without_audio_or_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, avatar = _files(tmp_path)
    output = tmp_path / "output" / script.stem
    args = Namespace(
        visual_style="profesional",
        script_file=None,
        scripts_dir=script.parent,
        script=script.name,
        list_scripts=False,
        avatars_dir=avatar.parent,
        avatar=avatar.name,
        list_avatars=False,
        output=tmp_path / "output",
        max_parallel_calls=1,
        from_stage="B1.1",
        stop_stage="B1.1",
        skip_images=False,
        no_audio=False,
        images_only=False,
        regenerate_audio=False,
        resume_audio=False,
    )
    requested: list[str] = []

    class FakeMeteredProvider:
        async def generate_structured_with_response(self, **kwargs):
            payload = json.loads(kwargs["input_text"])
            output_type = kwargs["output_type"]
            requested.append(output_type.__name__)
            if output_type is B11Output:
                result = B11Output.model_validate(_b11_payload())
            elif output_type is WholeB12Output:
                result = WholeB12Output.model_validate({
                    "pipeline_stage": "B1.2",
                    "narrative_core": payload["narrative_core"],
                    "blocks": [{
                        "block_id": 1,
                        "beats": [{
                            "beat_id": "1A", "text": payload["blocks"][0]["text"],
                            "beat_type": "orientation",
                        }],
                    }],
                    "error": None,
                })
            else:
                from ai_video_factory.bots.contracts import ProfessionalB2BatchOutput

                assert output_type is ProfessionalB2BatchOutput
                block = payload["blocks"][0]
                block["beats"][0].update(
                    visual_type="avatar", prompt=None, video_prompt=None,
                    reference=False, audio=True,
                )
                result = ProfessionalB2BatchOutput.model_validate({
                    "pipeline_stage": "B2",
                    "narrative_core": payload["narrative_core"],
                    "blocks": [block],
                    "error": None,
                })
            response = SimpleNamespace(
                id=f"response-{len(requested)}",
                model="gpt-6-luna",
                service_tier="default",
                usage=SimpleNamespace(
                    input_tokens=100,
                    input_tokens_details=SimpleNamespace(
                        cached_tokens=0, cache_write_tokens=0
                    ),
                    output_tokens=20,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                ),
            )
            return result, response

    def forbid_paid(*_args, **_kwargs):
        pytest.fail("--stop must not start Fish Audio or image requests")

    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner, "ensure_avatar_qr", _fake_avatar_qr)
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-openai-key")
    monkeypatch.setattr(runner.settings, "ai33_api_key", None)
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **_kw: FakeMeteredProvider())
    monkeypatch.setattr(runner, "_generate_audio", forbid_paid)
    monkeypatch.setattr(runner, "_generate_images", forbid_paid)

    for start, stop, expected, present, absent in (
        ("B1.1", "B1.1", ["B11Output"], ["B1.1/output.json"], ["B1.2", "B2"]),
        (
            "B1.2", "B1.2", ["B11Output", "WholeB12Output"],
            ["B1.2/merged_output.json"], ["B2", "visual_plan.json"],
        ),
        (
            "B2", "B2", ["B11Output", "WholeB12Output", "ProfessionalB2BatchOutput"],
            ["B2/merged_output.json", "visual_plan.json"], ["images"],
        ),
    ):
        args.from_stage = start
        args.stop_stage = stop
        asyncio.run(runner.main())
        report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
        assert report["run"]["status"] == "completed"
        assert report["run"]["stopped_after"] == stop
        assert report["api_costs"]["pricing_status"] == "complete"
        assert report["api_costs"]["stages"]["B2"]["expected_calls"] == (
            1 if stop == "B2" else 0
        )
        assert requested == expected
        for relative in present:
            assert (output / relative).is_file()
        for relative in absent:
            assert not (output / relative).exists()
        assert not (output / "audio").exists()



def test_preflight_regenerates_same_gemini_group_after_stt_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = {
        "status": "completed",
        "provider": "gemini",
        "stt_complete": True,
        "blocks": [],
    }
    plan = {"blocks": [{"block_id": 3, "beats": []}]}
    checks = 0
    regenerations: list[int] = []

    def fake_preflight(_output, _plan, candidate):
        nonlocal checks
        checks += 1
        if not candidate.get("regenerated"):
            raise runner.SttAlignmentError(3, "repeatable Gemini mismatch")
        return {"status": "completed"}

    async def fake_retranscribe(*_args, **_kwargs):
        raise runner.SttRetryLimitReached(
            "STT block 3 already used both attempts for this audio"
        )

    async def fake_regenerate(
        output, saved_plan, candidate, block_id, *, gemini_api_key, stt_client,
    ):
        assert output == tmp_path
        assert saved_plan is plan
        assert block_id == 3
        assert gemini_api_key == "gemini-key"
        assert stt_client is stt
        regenerations.append(block_id)
        candidate["regenerated"] = True
        return 1

    stt = object()
    monkeypatch.setattr(runner, "_preflight_stt_alignment", fake_preflight)
    monkeypatch.setattr(runner, "retranscribe_audio_block", fake_retranscribe)
    monkeypatch.setattr(
        runner, "regenerate_professional_audio_block", fake_regenerate,
    )
    monkeypatch.setattr(runner, "OpenAISttClient", lambda *, api_key: stt)
    monkeypatch.setattr(runner.settings, "openai_api_key", "openai-key")
    monkeypatch.setattr(runner.settings, "gemini_api_key", "gemini-key")

    result = asyncio.run(runner._preflight_stt_alignment_with_retries(
        tmp_path,
        plan,
        audio,
        allow_audio_regeneration=False,
    ))

    assert result == {"status": "completed"}
    assert checks == 2
    assert regenerations == [3]
