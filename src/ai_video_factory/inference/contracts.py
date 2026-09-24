from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

DEFAULT_INFERENCE_MAX_ATTEMPTS = 5

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
_TASK_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_object_key(value: str) -> str:
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError("object keys must be non-empty relative POSIX paths")
    parts = PurePosixPath(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("object keys may not contain empty, '.' or '..' path segments")
    return value


class ObjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=2, max_length=128)
    key: str = Field(min_length=1, max_length=1024)
    sha256: str | None = None
    content_type: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("input names may contain only letters, numbers, '.', '_' and '-'")
        return value

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _validate_object_key(value)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        return normalized


class ObjectOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=1024)
    content_type: str = Field(min_length=1, max_length=255)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _validate_object_key(value)


class InferenceJobRequest(BaseModel):
    """Provider-neutral inference envelope persisted by the application job transport."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    job_id: str = Field(min_length=2, max_length=128)
    task: str = Field(min_length=2, max_length=128)
    inputs: list[ObjectInput] = Field(default_factory=list, max_length=16)
    output: ObjectOutput
    sidecar_outputs: dict[str, ObjectOutput] | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=100)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def effective_max_attempts(self) -> int:
        """Bound retries without changing fingerprints for legacy requests that omit the field."""

        return self.max_attempts or DEFAULT_INFERENCE_MAX_ATTEMPTS

    @field_validator("job_id")
    @classmethod
    def validate_job_id(cls, value: str) -> str:
        if not _IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("job_id may contain only letters, numbers, '.', '_' and '-'")
        return value

    @field_validator("task")
    @classmethod
    def validate_task(cls, value: str) -> str:
        if not _TASK_PATTERN.fullmatch(value):
            raise ValueError("task must be a lowercase dotted identifier")
        return value

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        names = [item.name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("input names must be unique")

        expected_prefix = f"jobs/{self.job_id}/"
        if not self.output.key.startswith(expected_prefix):
            raise ValueError(f"output.key must start with {expected_prefix!r}")

        sidecars = self.sidecar_outputs or {}
        invalid_names = [name for name in sidecars if not _IDENTIFIER_PATTERN.fullmatch(name)]
        if invalid_names:
            raise ValueError("sidecar output names must be valid identifiers")
        output_keys = [self.output.key]
        for sidecar in sidecars.values():
            if not sidecar.key.startswith(expected_prefix):
                raise ValueError(
                    f"sidecar output keys must start with {expected_prefix!r}"
                )
            output_keys.append(sidecar.key)
        if len(output_keys) != len(set(output_keys)):
            raise ValueError("output and sidecar output keys must be unique")
        return self

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class OutputArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    content_type: str
    size_bytes: int = Field(ge=1)
    sha256: str
    etag: str | None = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _validate_object_key(value)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        return normalized


class InferenceJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    job_id: str
    status: Literal["succeeded"] = "succeeded"
    request_sha256: str
    output: OutputArtifact
    attempt_count: int = Field(ge=1)
    replayed: bool = False
