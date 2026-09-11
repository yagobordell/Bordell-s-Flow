"""Backward-compatible alias for the provider-neutral inference HTTP app."""

from ai_video_factory.inference.app import create_app

__all__ = ["create_app"]
