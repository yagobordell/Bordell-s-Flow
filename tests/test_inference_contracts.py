import pytest
from pydantic import ValidationError

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput


def build_request(**updates: object) -> InferenceJobRequest:
    values: dict[str, object] = {
        "job_id": "job-001",
        "task": "infrastructure.copy",
        "inputs": [ObjectInput(name="source", key="inputs/source.txt")],
        "output": ObjectOutput(key="jobs/job-001/output.txt", content_type="text/plain"),
        "parameters": {"alpha": 1, "nested": {"z": 2, "a": 3}},
    }
    values.update(updates)
    return InferenceJobRequest.model_validate(values)


def test_request_fingerprint_is_canonical() -> None:
    first = build_request(parameters={"alpha": 1, "nested": {"z": 2, "a": 3}})
    second = build_request(parameters={"nested": {"a": 3, "z": 2}, "alpha": 1})

    assert first.fingerprint() == second.fingerprint()


def test_output_must_be_scoped_to_job_id() -> None:
    with pytest.raises(ValidationError, match="output.key must start"):
        build_request(output=ObjectOutput(key="jobs/other/output.txt", content_type="text/plain"))


def test_input_names_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="input names must be unique"):
        build_request(
            inputs=[
                ObjectInput(name="source", key="inputs/one.txt"),
                ObjectInput(name="source", key="inputs/two.txt"),
            ]
        )


def test_object_keys_reject_parent_segments() -> None:
    with pytest.raises(ValidationError, match="may not contain"):
        ObjectInput(name="source", key="inputs/../secret.txt")
