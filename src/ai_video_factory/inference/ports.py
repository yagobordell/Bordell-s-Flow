from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .contracts import InferenceJobRequest


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    content_type: str
    size_bytes: int
    etag: str | None
    metadata: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ObjectCreateResult:
    stored: StoredObject
    created: bool


class ObjectStorage(Protocol):
    def download(self, key: str, destination: Path) -> StoredObject: ...

    def upload(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> StoredObject: ...

    def create_if_absent(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> ObjectCreateResult: ...

    def stat(self, key: str) -> StoredObject | None: ...

    def ping(self) -> None: ...


class ClaimDecision(StrEnum):
    START = "start"
    REPLAY = "replay"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class JobClaim:
    decision: ClaimDecision
    attempt_count: int
    result: Mapping[str, Any] | None = None


class JobRepository(Protocol):
    def claim(
        self,
        request: InferenceJobRequest,
        request_sha256: str,
        *,
        owner: str,
        lease_seconds: int,
        transport_job_id: str | None,
        instance_id: str | None = None,
    ) -> JobClaim: ...

    def reconcile_recovered_success(
        self,
        request: InferenceJobRequest,
        request_sha256: str,
        *,
        result: Mapping[str, Any],
    ) -> JobClaim | None: ...

    def renew_lease(
        self,
        job_id: str,
        request_sha256: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> bool: ...

    def mark_succeeded(
        self,
        job_id: str,
        request_sha256: str,
        *,
        owner: str,
        result: Mapping[str, Any],
    ) -> None: ...

    def mark_failed(
        self,
        job_id: str,
        request_sha256: str,
        *,
        owner: str,
        error: str,
        retryable: bool,
    ) -> None: ...

    def next_pending_request(
        self,
        task_names: tuple[str, ...],
    ) -> InferenceJobRequest | None: ...

    def is_instance_draining(self, instance_id: str) -> bool: ...

    def ping(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LocalSidecarArtifact:
    name: str
    path: Path
    content_type: str


@dataclass(frozen=True, slots=True)
class LocalArtifact:
    path: Path
    content_type: str
    sidecars: tuple[LocalSidecarArtifact, ...] = ()


class TaskRunner(Protocol):
    task_name: str

    def run(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact: ...
