"""Backward-compatible aliases for inference job repositories."""

from ai_video_factory.inference.repository import InMemoryJobRepository, PostgresJobRepository

__all__ = ["InMemoryJobRepository", "PostgresJobRepository"]
