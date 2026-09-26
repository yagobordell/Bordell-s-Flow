"""Verify Fish starts after B1.1 beside B1.2 and audio-only avoids B2/images."""

import asyncio
import hashlib
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts.pipeline import run_b_pipeline as runner

VOICE = "fishaudio_80e34d5e0b2b4577a486f3a77e357261"


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


def test_images_start_after_b2_without_waiting_for_fish_narration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, avatar = _files(tmp_path)
    audio_started = asyncio.Event()
    audio_released = asyncio.Event()
    order = []
    args = Namespace(
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
        return {"status": "completed", "voice_id": VOICE, "file": "audio/ready.mp3"}

    async def fake_b(_source, **kwargs):
        kwargs["on_output"]("B1.1", None, _b11_payload())
        await asyncio.wait_for(audio_started.wait(), 2)
        order.append("b2_completed")
        return SimpleNamespace(
            b12=[SimpleNamespace(block_id=1)],
            visual_plan=lambda: {"blocks": []},
        )

    async def fake_images(destination, _plan):
        assert (destination / "visual_plan.json").is_file()
        assert audio_started.is_set() and not audio_released.is_set()
        order.append("images_started")
        audio_released.set()
        return {"model_id": "gpt-image-2.5-flare", "items": []}

    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-openai-key")
    monkeypatch.setattr(runner.settings, "ai33_api_key", "test-ai33-key")
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **_kw: object())
    monkeypatch.setattr(runner, "run_b_pipeline", fake_b)
    monkeypatch.setattr(runner, "_generate_audio", fake_audio)
    monkeypatch.setattr(runner, "_generate_images", fake_images)
    asyncio.run(runner.main())
    assert order == [
        "audio_started",
        "b2_completed",
        "images_started",
        "audio_completed",
    ]
