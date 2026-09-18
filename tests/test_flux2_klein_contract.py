from ai_video_factory.providers.salad_flux import build_flux_job_request, render_flux_prompt
from ai_video_factory.workers.flux2_klein import (
    FLUX2_KLEIN_MODEL_ID,
    FLUX2_KLEIN_MODEL_REVISION,
    FLUX2_KLEIN_REFERENCE_TASK,
    flux2_klein_application_job_id,
    flux2_klein_seed_for_job,
)


def test_flux2_klein_job_id_and_seed_are_deterministic() -> None:
    prompt = "A distant desert mountain range"
    first = flux2_klein_application_job_id(
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
        prompt=prompt,
        width=1024,
        height=1024,
    )
    second = flux2_klein_application_job_id(
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
        prompt=prompt,
        width=1024,
        height=1024,
    )
    assert first == second
    assert first.startswith("flux2-klein-reference-")
    assert flux2_klein_seed_for_job(first) == flux2_klein_seed_for_job(second)


def test_flux2_klein_request_uses_pinned_model_identity_and_recommended_sampling() -> None:
    canonical = (
        '{"high_level_description":"Canonical location reference. A desert ridge.",'
        '"style_description":{"lighting":"neutral daylight"},'
        '"compositional_deconstruction":{"background":"Distant mountains.","elements":[]}}'
    )
    rendered = render_flux_prompt(canonical)
    request = build_flux_job_request(
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
        prompt=canonical,
        model_id=FLUX2_KLEIN_MODEL_ID,
        width=1024,
        height=1024,
    )

    assert request.parameters["prompt"] == rendered
    assert request.parameters["model_revision"] == FLUX2_KLEIN_MODEL_REVISION
    assert request.parameters["num_inference_steps"] == 4
    assert request.parameters["guidance_scale"] == 1.0
    assert request.output.key == f"jobs/{request.job_id}/image.png"
