"""Backward-compatible aliases for inference infrastructure errors."""

from ai_video_factory.inference.errors import (
    InferenceInfrastructureError,
    InputIntegrityError,
    JobBusyError,
    JobConflictError,
    JobExecutionError,
    LeaseLostError,
    OutputConflictError,
    UnsupportedTaskError,
)

GPUInfrastructureError = InferenceInfrastructureError

__all__ = [
    "GPUInfrastructureError",
    "InputIntegrityError",
    "JobBusyError",
    "JobConflictError",
    "JobExecutionError",
    "LeaseLostError",
    "OutputConflictError",
    "UnsupportedTaskError",
]
