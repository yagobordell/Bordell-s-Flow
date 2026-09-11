"""Backward-compatible imports for the dedicated LTX-2.5 worker."""

from ai_video_factory.workers.ltx25.model import (
    LTX_GENERATION_PROFILE,
    LTX_VIDEO_TASK,
    DirectLTX25Backend,
    LTXModelFiles,
    LTXVideoBackend,
    LTXVideoParameters,
    LTXVideoTaskRunner,
    ltx_num_frames_for_duration,
)

__all__ = [
    "DirectLTX25Backend",
    "LTX_GENERATION_PROFILE",
    "LTX_VIDEO_TASK",
    "LTXModelFiles",
    "LTXVideoBackend",
    "LTXVideoParameters",
    "LTXVideoTaskRunner",
    "ltx_num_frames_for_duration",
]
