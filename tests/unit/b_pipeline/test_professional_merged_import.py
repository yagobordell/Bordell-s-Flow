"""Offline regression tests for importing a finished professional B2 TXT."""
from __future__ import annotations

import asyncio
import hashlib
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.bots.professional_import import load_imported_professional_b2
from scripts.pipeline import run_b_pipeline as runner


def _merged() -> dict:
    return {
        "schema_version": "b-pipeline-merged-v1",
        "pipeline_stage": "B2",
        "visual_style": "profesional",
        "narrative_core": {
            "central_question": "¿Qué pasó?",
            "final_answer": "Lo explicamos.",
        },
        "blocks": [
            {
                "block_id": 1,
                "type": "intro",
                "emotional_entry": "curiosidad",
                "emotional_exit": "atención",
                "span": {"first_words": "Uno.", "last_words": "Uno."},
                "beats": [
                    {
                        "beat_id": "1A",
                        "text": "Uno. ",
                        "beat_type": "setup",
                        "visual_type": "avatar",
                        "prompt": None,
                        "video_prompt": None,
                        "reference": False,
                        "audio": True,
                    }
                ],
            },
            {
                "block_id": 2,
                "type": "close",
                "emotional_entry": "certeza",
                "emotional_exit": "resolución",
                "span": {"first_words": "Dos.", "last_words": "Dos."},
                "beats": [
                    {
                        "beat_id": "2A",
                        "text": "Dos.",
                        "beat_type": "return",
                        "visual_type": "avatar",
                        "prompt": None,
                        "video_prompt": None,
                        "reference": False,
                        "audio": True,
                    }
                ],
            },
        ],
    }


def test_professional_merged_import_roundtrips_beats_without_b_checkpoints() -> None:
    merged = _merged()
    loaded = load_imported_professional_b2(json.dumps(merged, ensure_ascii=False))
    assert loaded is not None
    assert loaded.merged_output == merged
    assert loaded.narration == "Uno. Dos."
    assert loaded.cta_groups == ()


def test_regular_scripts_are_unchanged() -> None:
    assert load_imported_professional_b2("Este es un guion original.") is None
    assert load_imported_professional_b2('{"unrelated": "json"}') is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda x: x.update(visual_style="casero"), "visual_style"),
        (lambda x: x["blocks"][1].update(block_id=3), "consecutive"),
        (lambda x: x["blocks"][1]["beats"][0].update(beat_id="1A"), "invalid beat ID"),
        (lambda x: x["blocks"][1]["beats"][0].update(audio=False), "Presenter visuals"),
        (lambda x: x["blocks"][1]["beats"][0].update(prompt="otra"), "avatar"),
        (lambda x: x["blocks"][1].update(type="other"), "validation"),
    ],
)
def test_invalid_merged_b2_is_rejected_before_inference(change, message: str) -> None:
    merged = _merged()
    change(merged)
    with pytest.raises(ValueError, match=message):
        load_imported_professional_b2(json.dumps(merged))


def test_invalid_merged_json_is_not_treated_as_plain_script() -> None:
    with pytest.raises(ValueError, match="Malformed merged B2"):
        load_imported_professional_b2('{"schema_version":"b-pipeline-merged-v1",oops}')


def test_imported_cta_groups_and_ebook_reference_contract() -> None:
    merged = _merged()
    beat = merged["blocks"][0]["beats"][0]
    beat.update(
        visual_type="media_image",
        prompt="A photographic book on a wooden table, clean studio lighting",
        video_prompt=None,
        reference=True,
        audio=False,
        cta_group_id=1,
        cta_reference_sources=["ebook"],
    )
    loaded = load_imported_professional_b2(json.dumps(merged))
    assert loaded is not None
    assert loaded.cta_groups == (("1A",),)

    beat["cta_reference_sources"] = ["presenter"]
    with pytest.raises(ValueError, match="ebook reference"):
        load_imported_professional_b2(json.dumps(merged))


def test_imported_b2_flows_to_voice_and_images_without_any_b_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "input" / "video04.txt"
    script.parent.mkdir()
    raw = json.dumps(_merged(), ensure_ascii=False).encode("utf-8")
    script.write_bytes(raw)
    avatar = tmp_path / "avatars" / "enzo.png"
    avatar.parent.mkdir()
    Image.new("RGB", (24, 24), (40, 50, 60)).save(avatar)
    output_root = tmp_path / "work"
    args = Namespace(
        script_file=script, script=None, scripts_dir=script.parent,
        list_scripts=False, avatars_dir=avatar.parent, avatar="enzo",
        list_avatars=False, qr_only=False, youtube_only=False,
        publish_only=False, videos_only=False, images_only=False,
        regenerate_audio=False, resume_audio=False, output=output_root,
        results_root=tmp_path / "results", managed_target=None,
        max_parallel_calls=2, from_stage="B1.1", stop_stage=None,
        visual_style=None, no_audio=False, no_video=True, skip_images=False,
        tts_provider=None, voice_audio=None, voice_id=None,
        default_voice=True, replace_professional_voice=False,
        ebook_cover=None, qr_url=None, subtitles=None, cta_motion=None,
    )
    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-openai")
    monkeypatch.setattr(runner.settings, "gemini_api_key", "test-gemini")
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(runner, "_recover_avatar_layout", lambda path, *_a, **_k: path)
    monkeypatch.setattr(runner, "organize_avatar", lambda path, *_a, **_k: path)
    monkeypatch.setattr(runner, "ensure_avatar_meta", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner, "ensure_professional_avatar_voice",
        lambda **_kw: SimpleNamespace(
            snapshot=lambda: {"provider": "gemini", "voice_id": "Algenib"}
        ),
    )
    monkeypatch.setattr(
        runner, "ensure_avatar_qr",
        lambda **_kw: SimpleNamespace(
            png=b"fake-qr",
            sha256=hashlib.sha256(b"fake-qr").hexdigest(),
            url="https://example.com",
            generator="test",
        ),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("No B planning bot may run on imported B2")

    monkeypatch.setattr(runner, "run_b_pipeline", forbidden)
    seen: list[str] = []

    async def fake_voice(**kwargs):
        seen.append("V1-V4")
        assert kwargs["core"] == _merged()["narrative_core"]
        assert kwargs["blocks"] == _merged()["blocks"]
        assert (kwargs["output"] / "B2" / "merged_output.json").exists()
        return {
            "status": "completed",
            "provider": "gemini",
            "voice_id": "Algenib",
            "file": "audio/narracion.wav",
            "sha256": "x" * 64,
        }

    async def fake_images(_output, plan):
        seen.append("images")
        assert plan["visual_style"] == "profesional"
        assert plan["blocks"] == _merged()["blocks"]
        return {"model_id": "test", "items": [{"provider": "openai_direct"}]}

    async def skip_preflight(*_a, **_k):
        return False

    async def skip_preview(*_a, **_k):
        return None

    monkeypatch.setattr(runner, "generate_professional_audio", fake_voice)
    monkeypatch.setattr(runner, "_generate_images", fake_images)
    monkeypatch.setattr(runner, "_preflight_saved_run_audio", skip_preflight)
    monkeypatch.setattr(runner, "_preview_if_ready", skip_preview)
    monkeypatch.setattr(runner, "_publish_if_ready", forbidden)

    # Successful media staging is enough to establish this route. No paid API runs.
    # Disable publication in this offline simulation.
    monkeypatch.setattr(runner, "_publish_if_ready", lambda *_a, **_k: None)
    asyncio.run(runner.main())
    output = output_root / "video04"
    report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    plan = json.loads((output / "visual_plan.json").read_text(encoding="utf-8"))
    assert seen == ["V1-V4", "images"]
    assert not (output / "B1.1").exists()
    assert not (output / "B1.2").exists()
    assert not (output / "B2.1").exists()
    assert (output / "source_narration.txt").read_text(encoding="utf-8") == "Uno. Dos."
    assert (output / "B2" / "merged_output.json").is_file()
    assert plan["visual_style"] == "profesional"
    assert report["run"]["input_kind"] == "professional_merged_b2"
    assert report["run"]["script_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["run"]["status"] == "completed"
    for stage in ("B1.1", "B1.2", "B2", "B2.1"):
        assert report["api_costs"]["stages"][stage]["expected_calls"] == 0
        assert report["api_costs"]["stages"][stage]["recorded_calls"] == 0
    assert script.read_bytes() == raw


def test_imported_voice_resume_does_not_require_b11(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "input" / "video05.txt"
    script.parent.mkdir()
    raw = json.dumps(_merged(), ensure_ascii=False).encode("utf-8")
    script.write_bytes(raw)
    output = tmp_path / "work" / "video05"
    (output / "B2").mkdir(parents=True)
    (output / "B2" / "merged_output.json").write_bytes(raw)
    (output / "avatar_voice.json").write_text(
        json.dumps({"voice_id": "Algenib", "provider": "gemini"}), encoding="utf-8"
    )
    expected = {
        "B1.1": 0, "B1.2": 0, "B2": 0, "B2.1": 0,
    }
    runner._write(output / "run_report.json", {
        "run": {
            "visual_style": "profesional",
            "input_kind": "professional_merged_b2",
            "script_sha256": hashlib.sha256(raw).hexdigest(),
            "status": "running",
            "audio_generation": {"status": "incomplete"},
        },
        "timings": runner.TimingLedger().report(run_seconds=0.1, status="running"),
        "api_costs": runner.ApiCostLedger().report(
            blocks=2, run_status="running", expected_calls=expected,
        ),
    })
    monkeypatch.setattr(runner.settings, "openai_api_key", "test-openai")
    monkeypatch.setattr(runner.settings, "gemini_api_key", "test-gemini")
    monkeypatch.setattr(runner, "OpenAIProvider", lambda **_kw: object())

    async def fake_audio(**kwargs):
        assert kwargs["core"] == _merged()["narrative_core"]
        assert kwargs["blocks"] == _merged()["blocks"]
        assert not (output / "B1.1").exists()
        return {
            "status": "completed", "provider": "gemini",
            "voice_id": "Algenib", "sha256": "a" * 64,
            "file": "audio/narracion.wav",
        }

    async def no_preview(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runner, "generate_professional_audio", fake_audio)
    monkeypatch.setattr(runner, "_preview_if_ready", no_preview)
    monkeypatch.setattr(runner, "_publish_if_ready", lambda *_a, **_kw: None)
    asyncio.run(runner._run_audio_only(
        script, tmp_path / "work", resume=True, no_video=True,
    ))
    report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    assert report["run"]["audio_generation"]["status"] == "completed"
    for stage in expected:
        assert report["api_costs"]["stages"][stage]["expected_calls"] == 0
