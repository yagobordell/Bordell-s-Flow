"""Backward-compatible aliases for provider-neutral inference ports."""

from ai_video_factory.inference.ports import (
    ClaimDecision,
    JobClaim,
    JobRepository,
    LocalArtifact,
    ObjectStorage,
    StoredObject,
    TaskRunner,
)

__all__ = [
    "ClaimDecision",
    "JobClaim",
    "JobRepository",
    "LocalArtifact",
    "ObjectStorage",
    "StoredObject",
    "TaskRunner",
]
