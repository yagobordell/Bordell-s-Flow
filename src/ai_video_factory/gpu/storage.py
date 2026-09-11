"""Backward-compatible aliases for inference object storage."""

from ai_video_factory.inference.storage import LocalObjectStorage, R2ObjectStorage, sha256_file

__all__ = ["LocalObjectStorage", "R2ObjectStorage", "sha256_file"]
