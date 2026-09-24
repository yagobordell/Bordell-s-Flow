from ai_video_factory.providers.salad_qwen_image import build_qwen_image_job_request
from ai_video_factory.workers.qwen_image_21.model import (
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_MODEL_ID,
)
from scripts.smoke.run_qwen_bf16_recovery import build_recovery_request


def test_recovery_clones_prompt_seed_and_steps_to_uncached_one_attempt_job() -> None:
    source = build_qwen_image_job_request(
        task_name=QWEN_IMAGE_21_KEYFRAME_TASK,
        prompt="a compact robotic cinema camera on a studio table",
        model_id=QWEN_IMAGE_21_MODEL_ID,
        width=1280,
        height=736,
    )

    recovered = build_recovery_request(
        source, job_id="qwen-bf16-recovery-new-test-0001"
    )

    assert recovered.job_id != source.job_id
    assert recovered.output.key == f"jobs/{recovered.job_id}/image.png"
    assert recovered.task == source.task
    assert recovered.parameters == source.parameters
    assert recovered.max_attempts == 1
    assert recovered.fingerprint() != source.fingerprint()
    assert recovered.output.key != source.output.key
