"""Cost-free, scripted Casero run simulation against the production contracts.

The fictitious narrative exercises whole-video B1.2, per-block Casero B2,
independent CTA placement, saved word-level STT, post-CTA subtitle timing,
avatar-scoped motion selection and --generate-video resume semantics.
No OpenAI, Salad, R2, Postgres, TTS, GPU or real FFmpeg request is issued.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.bots.contracts import B2Output, B11Output, WholeB12Output
from ai_video_factory.bots.cta import CTAPlacement
from ai_video_factory.bots.workflow import run_b_pipeline
from ai_video_factory.compositor import casero_subtitles
from ai_video_factory.providers import avatar_cta_motion
from scripts.pipeline import run_b_pipeline as runner


FIRST = "Una lámpara encendida consume electricidad."
SECOND = "Apagarla reduce el gasto cuando no se usa."
THIRD = "Así, un gesto cotidiano evita desperdiciar energía."
SCRIPT = f"{FIRST}\n\n{SECOND}\n\n{THIRD}"


def _sha(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


class OfflineCaseroBots:
    """All responses reproduce strict bot schemas but incur zero paid calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.b12_payloads: list[dict] = []
        self.b2_payloads: list[dict] = []
        self.inputs: list[dict] = []

    async def generate_structured(
        self, *, model, instructions, input_text, output_type, input_image_url=None,
    ):
        payload = json.loads(input_text)
        self.calls.append(output_type.__name__)
        if output_type is CTAPlacement:
            assert payload["script_for_recording"] == SCRIPT
            return CTAPlacement(previous_sentence=FIRST, next_sentence=SECOND)
        if output_type is B11Output:
            assert payload["plain_script_for_recording"] == SCRIPT
            return B11Output.model_validate({
                "pipeline_stage": "B1.1",
                "narrative_core": {
                    "central_question": "¿Cómo evitar desperdiciar energía?",
                    "final_answer": "Apagar los dispositivos que no necesitamos.",
                },
                "blocks": [
                    {
                        "block_id": index,
                        "type": kind,
                        "emotional_entry": "curiosidad",
                        "emotional_exit": "comprensión",
                        "span": {"first_words": text, "last_words": text},
                    }
                    for index, (kind, text) in enumerate((
                        ("intro", FIRST), ("development", SECOND),
                        ("close", THIRD),
                    ), start=1)
                ],
                "error": None,
            })
        if output_type is WholeB12Output:
            self.b12_payloads.append(payload)
            assert payload["plain_script_for_recording"] == SCRIPT
            assert len(payload["blocks"]) == 3
            return WholeB12Output.model_validate({
                "pipeline_stage": "B1.2",
                "narrative_core": payload["narrative_core"],
                "blocks": [
                    {"block_id": block["block_id"], "beats": [{
                        "beat_id": f"{block['block_id']}A",
                        "text": block["text"],
                        "beat_type": "orientation",
                    }]}
                    for block in payload["blocks"]
                ],
                "error": None,
            })
        assert output_type is B2Output
        assert len(self.b12_payloads) == 1, "Casero B2 must follow global B1.2"
        self.b2_payloads.append(payload)
        assert len(payload["blocks"]) == 1, "Casero B2 still uses per-block calls"
        block = payload["blocks"][0]
        assert block["block_id"] == payload["current_block_id"]
        kind = "media_image" if block["block_id"] == 2 else "avatar"
        for beat in block["beats"]:
            beat.update(
                visual_type=kind,
                description=(
                    "Plano de una lámpara apagada sobre una mesa"
                    if kind == "media_image" else None
                ),
            )
        return B2Output.model_validate({
            "pipeline_stage": "B2",
            "narrative_core": payload["narrative_core"],
            "blocks": [block],
            "error": None,
        })


def test_fictitious_casero_script_runs_global_b12_and_per_block_b2_with_cta() -> None:
    fake = OfflineCaseroBots()
    returned: dict[str, object] = {}
    result = asyncio.run(run_b_pipeline(
        SCRIPT, provider=fake, visual_style="casero",
        avatar_image=b"synthetic-avatar-png", enable_casero_cta=True,
        on_output=lambda stage, block, data: returned.__setitem__(
            f"{stage}:{block}", data,
        ),
    ))

    assert fake.calls.count("B11Output") == 1
    assert fake.calls.count("WholeB12Output") == 1
    assert fake.calls.count("B2Output") == 3
    assert fake.calls.count("CTAPlacement") == 1
    assert len(fake.b2_payloads) == 3
    assert returned["CTA:None"]["insert_after_character"] == len(FIRST)
    assert "".join(
        beat.text for block in result.b12 for beat in block.beats
    ) == SCRIPT
    assert [
        beat.visual_type for output in result.b2
        for block in output.blocks for beat in block.beats
    ] == ["avatar", "media_image", "avatar"]


def _synthetic_audio(output: Path) -> dict:
    blocks = []
    lines = [
        [("Una", 0.10, 0.31), ("lámpara.", 0.32, 0.79)],
        [("Apagarla", 0.10, 0.48), ("ahorra.", 0.49, 0.90)],
        [("Así", 0.10, 0.35), ("evitamos", 0.36, 0.64),
         ("desperdicios.", 0.65, 1.05)],
    ]
    for index, words in enumerate(lines, start=1):
        audio_sha = str(index) * 64
        transcript = output / f"stt_block_{index}.json"
        transcript.write_text(json.dumps({
            "source_audio_sha256": audio_sha,
            "model": "whisper-1",
            "words": [
                {"word": word, "start": start, "end": end}
                for word, start, end in words
            ],
        }), encoding="utf-8")
        blocks.append({
            "block_id": index, "sha256": audio_sha,
            "stt": {"file": transcript.name, "sha256": _sha(transcript),
                    "model": "whisper-1"},
        })
    return {
        "sha256": "a" * 64, "stt_complete": True,
        "block_start_seconds": [0.0, 2.0, 4.0],
        "blocks": blocks,
    }


@pytest.mark.parametrize("motion", ["automatic", "manual"])
@pytest.mark.parametrize("with_subtitles", [False, True])
def test_offline_casero_motion_and_subtitle_combinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    motion: str, with_subtitles: bool,
) -> None:
    avatar = tmp_path / "avatar" / "enzo.png"
    avatar.parent.mkdir()
    avatar.write_bytes(b"fake-avatar-png")
    motion_dir = avatar.parent / "meta" / "cta"
    if motion == "manual":
        motion_dir.mkdir(parents=True)
        (motion_dir / "motion_manual.mp4").write_bytes(b"fake-manual-mp4")
        monkeypatch.setattr(avatar_cta_motion, "probe_manual_motion", lambda _path: 16.0)
    choice = avatar_cta_motion.ensure_avatar_cta_motion(
        avatar, motion, interactive=False,
    )
    assert choice["mode"] == motion
    assert avatar_cta_motion.motion_source(motion_dir)["mode"] == motion
    assert avatar_cta_motion.ensure_avatar_cta_motion(
        avatar, motion, interactive=False,
    ) == choice

    output = tmp_path / "work"
    run_dir = output / "video" / "runs" / "synthetic"
    run_dir.mkdir(parents=True)
    original = output / "after_cta.mp4"
    original.write_bytes(b"fake-verified-render-with-inserted-CTA")
    sound = _synthetic_audio(output)
    video = {
        "file": original.name, "sha256": _sha(original),
        "cta_insertion": {
            "source_boundary_seconds": 3.01,
            "video_cut_seconds": 3.0,
            "cta_duration_seconds": 5.0,
        },
    }
    if not with_subtitles:
        assert not (run_dir / "subtitles.srt").exists()
        assert not (run_dir / "with_subtitles.mp4").exists()
        return

    commands: list[list[str]] = []

    def fake_ffmpeg(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        assert kwargs["cwd"] == run_dir
        assert "ass=subtitles.ass" in command
        Path(command[-1]).write_bytes(b"local-ffmpeg-output-no-external-services")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(casero_subtitles.subprocess, "run", fake_ffmpeg)
    result = casero_subtitles.burn_casero_subtitles(output, run_dir, video, sound)
    assert result["subtitles"]["enabled"] is True
    assert result["subtitles"]["caption_count"] == 3
    srt = (run_dir / "subtitles.srt").read_text(encoding="utf-8")
    ass = (run_dir / "subtitles.ass").read_text(encoding="utf-8")
    assert "00:00:00,100 -->" in srt
    assert "00:00:02,100 -->" in srt
    assert "00:00:09,100 -->" in srt
    assert not any(
        start < 8 and end > 3
        for start, end in (
            (float(item["start"]), float(item["end"]))
            for group in casero_subtitles._shift_around_cta(
                casero_subtitles._read_words(output, sound),
                video["cta_insertion"],
            )
            for item in group
        )
    )
    assert "Style: Caption,Arial,30," in ass
    assert "\\N" not in ass
    assert original.read_bytes() == b"fake-verified-render-with-inserted-CTA"
    assert len(commands) == 1
    assert casero_subtitles.burn_casero_subtitles(
        output, run_dir, video, sound,
    ) == result
    assert len(commands) == 1, "Resume must reuse subtitle render"


def test_generate_video_resume_remembers_subtitles_and_blocks_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "input" / "simulado.txt"
    script.parent.mkdir()
    script.write_text(SCRIPT, encoding="utf-8")
    output_root = tmp_path / "result"
    work = output_root / script.stem
    preview = work / "preview" / "preview.mp4"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"valid-preview")
    (work / "visual_plan.json").write_text('{"blocks": []}', encoding="utf-8")
    runner._write(work / "run_report.json", {
        "run": {
            "visual_style": "casero", "script_sha256": _sha(script),
            "stt_alignment": {"status": "completed"},
            "audio_generation": {"status": "completed", "stt_complete": True},
            "video_preview": {
                "status": "completed", "file": "preview/preview.mp4",
                "sha256": _sha(preview),
            },
            "video_generation": {"status": "skipped"},
            "casero_subtitles": False,
        },
        "timings": {"status": "completed"},
        "api_costs": {"run_status": "completed"},
    })

    calls = []

    async def fake_ltx(output: Path, report: dict, *, require: bool = False) -> dict:
        assert output == work and require
        calls.append(report["run"]["casero_subtitles"])
        return {
            "status": "completed", "file": "video/render.mp4",
            "subtitles": {"enabled": True},
        }

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Resume repeated a paid planning/audio/image step")

    monkeypatch.setattr(runner, "_video_if_ready", fake_ltx)
    monkeypatch.setattr(runner, "_publish_if_ready", lambda *_args: None)
    monkeypatch.setattr(runner, "_generate_audio", forbidden)
    monkeypatch.setattr(runner, "_generate_images", forbidden)
    monkeypatch.setattr(runner, "run_b_pipeline", forbidden)
    asyncio.run(runner._run_videos_only(script, output_root, subtitles=True))
    assert calls == [True]
    saved = json.loads((work / "run_report.json").read_text(encoding="utf-8"))
    assert saved["run"]["casero_subtitles"] is True
    assert saved["run"]["video_generation"]["status"] == "completed"
    with pytest.raises(SystemExit, match="Cannot change subtitles"):
        asyncio.run(runner._run_videos_only(script, output_root, subtitles=False))
    assert calls == [True], "A completed run must not rerender on choice change"
