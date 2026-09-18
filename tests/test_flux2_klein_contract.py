from ai_video_factory.providers.salad_flux2 import (
    build_flux2_klein_job_request,
    render_flux2_klein_prompt,
)
from ai_video_factory.workers.flux2_klein import (
    FLUX2_KLEIN_GENERATION_PROFILE,
    FLUX2_KLEIN_MODEL_ID,
    FLUX2_KLEIN_MODEL_REVISION,
    FLUX2_KLEIN_REFERENCE_TASK,
    flux2_klein_application_job_id,
    flux2_klein_seed_for_job,
)


def test_flux2_klein_job_id_seed_and_model_identity_are_deterministic() -> None:
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

    different_revision = flux2_klein_application_job_id(
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
        prompt=prompt,
        width=1024,
        height=1024,
        model_revision="different-revision",
    )
    assert different_revision != first


def test_flux2_klein_request_uses_official_distilled_profile() -> None:
    canonical = (
        '{"high_level_description":"Canonical location reference. A desert ridge.",'
        '"style_description":{"lighting":"neutral daylight"},'
        '"compositional_deconstruction":{"background":"Distant mountains.","elements":[]}}'
    )
    rendered = render_flux2_klein_prompt(canonical)
    request = build_flux2_klein_job_request(
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
        prompt=canonical,
        model_id=FLUX2_KLEIN_MODEL_ID,
        width=1024,
        height=1024,
    )

    assert request.parameters["prompt"] == rendered
    assert request.parameters["generation_profile"] == FLUX2_KLEIN_GENERATION_PROFILE
    assert request.parameters["model_id"] == FLUX2_KLEIN_MODEL_ID
    assert request.parameters["model_revision"] == FLUX2_KLEIN_MODEL_REVISION
    assert request.parameters["num_inference_steps"] == 4
    assert request.parameters["guidance_scale"] == 1.0
    assert request.parameters["max_sequence_length"] == 512
    assert request.output.key == f"jobs/{request.job_id}/image.png"
