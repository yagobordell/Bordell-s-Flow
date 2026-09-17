from ai_video_factory.providers.salad_flux import build_flux_job_request, render_flux_prompt
from ai_video_factory.workers.flux_schnell import (
    FLUX_SCHNELL_MODEL_ID,
    FLUX_SCHNELL_REFERENCE_TASK,
    flux_application_job_id,
    flux_seed_for_job,
)


def test_flux_job_id_and_seed_are_deterministic() -> None:
    prompt = "A distant desert mountain range"
    first = flux_application_job_id(
        task_name=FLUX_SCHNELL_REFERENCE_TASK,
        prompt=prompt,
        width=1024,
        height=1024,
    )
    second = flux_application_job_id(
        task_name=FLUX_SCHNELL_REFERENCE_TASK,
        prompt=prompt,
        width=1024,
        height=1024,
    )
    assert first == second
    assert first.startswith("flux-reference-")
    assert flux_seed_for_job(first) == flux_seed_for_job(second)


def test_flux_request_uses_rendered_structured_prompt() -> None:
    canonical = (
        '{"high_level_description":"Canonical location reference. A desert ridge.",'
        '"style_description":{"lighting":"neutral daylight"},'
        '"compositional_deconstruction":{"background":"Distant mountains.","elements":[]}}'
    )
    rendered = render_flux_prompt(canonical)
    assert "desert ridge" in rendered
    assert "neutral daylight" in rendered
    assert "Distant mountains" in rendered

    request = build_flux_job_request(
        task_name=FLUX_SCHNELL_REFERENCE_TASK,
        prompt=canonical,
        model_id=FLUX_SCHNELL_MODEL_ID,
        width=1024,
        height=1024,
    )
    assert request.parameters["prompt"] == rendered
    assert request.parameters["num_inference_steps"] == 4
    assert request.output.key == f"jobs/{request.job_id}/image.png"
