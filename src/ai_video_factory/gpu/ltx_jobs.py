"""Backward-compatible LTX job identity helpers."""

from ai_video_factory.workers.ltx25.jobs import (
    ltx_a2v_application_job_id,
    ltx_video_application_job_id,
)

__all__ = ["ltx_a2v_application_job_id", "ltx_video_application_job_id"]
