"""Backward-compatible alias for the provider-neutral inference worker."""

from ai_video_factory.inference.worker import InferenceWorker

GPUWorker = InferenceWorker

__all__ = ["GPUWorker"]
