"""Phase 4: the real orchestrator calls B1.1, then Agent B, not legacy creative bots."""

import asyncio

import pytest

from ai_video_factory.bots.contracts import B11Output
from ai_video_factory.bots.workflow import run_b_pipeline


def _b11() -> B11Output:
    return B11Output.model_validate({
        "pipeline_stage": "B1.1",
        "narrative_core": {
            "central_question": "Why does a phone ring?",
            "final_answer": "A caller reached the phone.",
        },
        "blocks": [{
            "block_id": 1, "type": "intro", "emotional_entry": "uncertain",
            "emotional_exit": "curious", "span": {
                "first_words": "A phone rings.",
                "last_words": "A phone rings.",
            },
        }],
        "error": None,
    })


def _unified(b11):
    return {
        "schema_version": "b-pipeline-v1",
        "visual_style": "profesional",
        "narrative_core": b11.narrative_core.model_dump(mode="json"),
        "blocks": [{
            "block_id": 1, "type": "intro",
            "emotional_entry": "uncertain", "emotional_exit": "curious",
            "beats": [{
                "beat_id": "1A", "text": "A phone rings.",
                "beat_type": "action", "visual_type": "media_image",
                "prompt": "A phone buzzing on a table.",
                "video_prompt": None, "reference": False, "audio": False,
            }],
        }],
    }


def test_opt_in_orchestrator_skips_b12_b2_b21_creative_calls(tmp_path, monkeypatch):
    from ai_video_factory.bots import workflow
    from ai_video_factory.bots.agent_b import runner

    b11 = _b11()
    calls = []

    async def call_stage(stage, *args, **kwargs):
        calls.append(stage)
        if stage != "B1.1":
            raise AssertionError("Legacy creative bot was unexpectedly called")
        return b11

    async def fake_agent(project_dir, blocks, core, *, model):
        assert project_dir == tmp_path
        assert [block.text for block in blocks] == ["A phone rings."]
        assert core == b11.narrative_core.model_dump()
        return _unified(b11)

    async def detector(*args, **kwargs):
        calls.append("CTA Detector")
        return ()

    monkeypatch.setattr(workflow, "_call_stage", call_stage)
    monkeypatch.setattr(workflow, "detect_professional_ctas", detector)
    monkeypatch.setattr(runner, "run_agent_b", fake_agent)

    result = asyncio.run(run_b_pipeline(
        "A phone rings.", provider=object(), model="test-model",
        visual_style="profesional", avatar_image=b"avatar",
        ebook_image=b"ebook", stop_after="B2.1",
        agent_b_project_dir=tmp_path,
    ))
    assert calls == ["B1.1", "CTA Detector"]
    assert result.visual_plan() == {**_unified(b11), "planning_origin": "agent_b"}
    assert len(result.b12) == len(result.b2) == 1
    assert result.b2[0].blocks[0].span.last_words == "A phone rings."


def test_agent_b_opt_in_cannot_be_used_for_casero_or_resume(tmp_path):
    for style, stage in (("casero", "B1.1"), ("profesional", "B2")):
        with pytest.raises(ValueError, match="Agent B requires"):
            asyncio.run(run_b_pipeline(
                "A phone rings.", provider=object(), model="test-model",
                visual_style=style, stop_after="B2",
                start_from=stage, agent_b_project_dir=tmp_path,
            ))


def test_cta_disagreement_fails_closed_before_media(tmp_path, monkeypatch):
    from ai_video_factory.bots import workflow
    from ai_video_factory.bots.agent_b import runner

    b11 = _b11()

    async def call_stage(stage, *args, **kwargs):
        return b11

    async def fake_agent(project_dir, blocks, core, *, model):
        return _unified(b11)

    async def detector(*args, **kwargs):
        return (("1A",),)

    monkeypatch.setattr(workflow, "_call_stage", call_stage)
    monkeypatch.setattr(workflow, "detect_professional_ctas", detector)
    monkeypatch.setattr(runner, "run_agent_b", fake_agent)
    with pytest.raises(ValueError, match="disagree"):
        asyncio.run(run_b_pipeline(
            "A phone rings.", provider=object(), model="test-model",
            visual_style="profesional", avatar_image=b"avatar",
            ebook_image=b"ebook", stop_after="B2.1",
            agent_b_project_dir=tmp_path,
        ))
