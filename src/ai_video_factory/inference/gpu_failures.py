from __future__ import annotations

import gc

from .contracts import DEFAULT_INFERENCE_MAX_ATTEMPTS

DEFAULT_GPU_MAX_ATTEMPTS = DEFAULT_INFERENCE_MAX_ATTEMPTS
FATAL_GPU_CONTEXT_FAILURE_KINDS = frozenset({"cuda_context_lost"})
RETRYABLE_GPU_FAILURE_KINDS = frozenset(
    {"oom", "cuda_device", "cudnn_sdpa", *FATAL_GPU_CONTEXT_FAILURE_KINDS}
)


def classify_gpu_failure(error: BaseException) -> str | None:
    """Classify transient GPU failures using Media Pipeline's failure signatures."""

    for current in _exception_chain(error):
        message = str(current).lower()
        if "cannot fit a diffvae decode tile under the memory budget" in message:
            return "decoder_budget"
        if "cuda out of memory" in message or "out of memory" in message:
            return "oom"
        if any(
            signature in message
            for signature in (
                "cuda error: unknown error",
                "cudaerrorunknown",
                "illegal memory access",
                "device-side assert triggered",
                "unspecified launch failure",
                "cuda context is destroyed",
                "context is destroyed",
                "driver shutting down",
                "cuda error: initialization error",
            )
        ):
            return "cuda_context_lost"
        if "device not ready" in message:
            return "cuda_device"
        if "cudnn frontend error" in message or "no valid execution plans built" in message:
            return "cudnn_sdpa"
    return None


def is_retryable_gpu_failure(error: BaseException) -> bool:
    return classify_gpu_failure(error) in RETRYABLE_GPU_FAILURE_KINDS


def is_fatal_gpu_context_failure(error: BaseException) -> bool:
    """Return whether the live CUDA process must never execute another GPU job."""

    return classify_gpu_failure(error) in FATAL_GPU_CONTEXT_FAILURE_KINDS


def cleanup_cuda_memory() -> None:
    """Release Python and CUDA caches after a transient GPU failure."""

    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _exception_chain(error: BaseException):
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__
