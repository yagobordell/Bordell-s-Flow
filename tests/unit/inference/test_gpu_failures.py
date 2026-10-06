from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from ai_video_factory.inference.errors import JobExecutionError
from ai_video_factory.inference.gpu_failures import (
    classify_gpu_failure,
    cleanup_cuda_memory,
    is_fatal_gpu_context_failure,
    is_retryable_gpu_failure,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("CUDA out of memory while allocating tensor", "oom"),
        ("RuntimeError: out of memory", "oom"),
        (
            "ValueError: Cannot fit a DiffVAE decode tile under the memory budget: "
            "stage4_feature_bytes=1022361600, usable_bytes=0.",
            "decoder_budget",
        ),
        ("CUDA error: unknown error", "cuda_context_lost"),
        ("RuntimeError: cudaErrorUnknown", "cuda_context_lost"),
        ("CUDA error: an illegal memory access was encountered", "cuda_context_lost"),
        ("CUDA error: device-side assert triggered", "cuda_context_lost"),
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


def test_cuda_unknown_is_fatal_context_loss_and_retryable_on_replacement() -> None:
    error = RuntimeError("torch.AcceleratorError: CUDA error: unknown error")

    assert classify_gpu_failure(error) == "cuda_context_lost"
    assert is_fatal_gpu_context_failure(error) is True
    assert is_retryable_gpu_failure(error) is True


def test_diffvae_unsatisfiable_budget_is_not_retryable() -> None:
    error = ValueError("Cannot fit a DiffVAE decode tile under the memory budget")
    assert classify_gpu_failure(error) == "decoder_budget"
    assert is_retryable_gpu_failure(error) is False


def test_cleanup_cuda_memory_collects_cuda_cache(monkeypatch) -> None:
    calls: list[str] = []
    fake_cuda = SimpleNamespace(
        is_available=lambda: True,
        empty_cache=lambda: calls.append("empty_cache"),
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=fake_cuda))

    cleanup_cuda_memory()

    assert calls == ["empty_cache"]
