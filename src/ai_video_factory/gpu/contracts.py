"""Backward-compatible aliases for the provider-neutral inference contracts."""

from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    InferenceJobResponse,
    ObjectInput,
    ObjectOutput,
    OutputArtifact,
)

GPUJobRequest = InferenceJobRequest
GPUJobResponse = InferenceJobResponse

__all__ = [
    "GPUJobRequest",
    "GPUJobResponse",
    "ObjectInput",
    "ObjectOutput",
    "OutputArtifact",
]
