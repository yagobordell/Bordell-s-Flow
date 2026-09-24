from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from ai_video_factory.inference.errors import JobExecutionError
from ai_video_factory.inference.gpu_failures import (
    classify_gpu_failure,
    cleanup_cuda_memory,
    is_retryable_gpu_failure,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("CUDA out of memory while allocating tensor", "oom"),
        ("RuntimeError: out of memory", "oom"),
        ("CUDA error: device not ready", "cuda_device"),
        ("cuDNN frontend error: graph rejected", "cudnn_sdpa"),
        ("No valid execution plans built for scaled dot product attention", "cudnn_sdpa"),
        ("invalid tensor shape", None),
    ],
)
def test_classify_gpu_failure_matches_media_pipeline_signatures(
    message: str,
    expected: str | None,
) -> None:
    assert classify_gpu_failure(RuntimeError(message)) == expected


def test_gpu_failure_classification_walks_wrapped_exception_chain() -> None:
    cause = RuntimeError("CUDA error: device not ready")
    wrapped = JobExecutionError("execution failed")
    wrapped.__cause__ = cause

    assert classify_gpu_failure(wrapped) == "cuda_device"
    assert is_retryable_gpu_failure(wrapped) is True


def test_cleanup_cuda_memory_collects_cuda_cache(monkeypatch) -> None:
    calls: list[str] = []
    fake_cuda = SimpleNamespace(
        is_available=lambda: True,
        empty_cache=lambda: calls.append("empty_cache"),
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=fake_cuda))

    cleanup_cuda_memory()

    assert calls == ["empty_cache"]
