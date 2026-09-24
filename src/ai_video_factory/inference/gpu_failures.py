from __future__ import annotations

import gc

DEFAULT_GPU_MAX_ATTEMPTS = 5
RETRYABLE_GPU_FAILURE_KINDS = frozenset({"oom", "cuda_device", "cudnn_sdpa"})


def classify_gpu_failure(error: BaseException) -> str | None:
    """Classify transient GPU failures using Media Pipeline's failure signatures."""

    for current in _exception_chain(error):
        message = str(current).lower()
        if "cuda out of memory" in message or "out of memory" in message:
            return "oom"
        if "device not ready" in message:
            return "cuda_device"
        if "cudnn frontend error" in message or "no valid execution plans built" in message:
            return "cudnn_sdpa"
    return None


def is_retryable_gpu_failure(error: BaseException) -> bool:
    return classify_gpu_failure(error) in RETRYABLE_GPU_FAILURE_KINDS


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
