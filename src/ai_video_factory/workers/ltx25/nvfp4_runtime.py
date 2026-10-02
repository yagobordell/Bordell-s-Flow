"""Fail-closed Blackwell native NVFP4 preflight; never enabled by default."""

from __future__ import annotations

import os
from typing import Any


def require_nvfp4_runtime(torch_module: Any, device: str) -> None:
    """Reject mismatched Docker images or GPUs before loading quantized weights."""
    if os.environ.get("LTX_I2V_NVFP4_RUNTIME") != "enabled":
        raise RuntimeError(
            "NVFP4 requires the separate pinned Dockerfile.nvfp4 image. "
            "Standard Salad I2V worker does not provide native NVFP4 kernels."
        )
    if not str(device).startswith("cuda") or not torch_module.cuda.is_available():
        raise RuntimeError("NVFP4 requires a CUDA Blackwell GPU")
    capability = torch_module.cuda.get_device_capability(torch_module.device(device))
    if tuple(capability) < (10, 0):
        raise RuntimeError(f"NVFP4 requires Blackwell (SM>=10); got {capability}")
    try:
        from ltx_kernels import nvfp4
    except ImportError as exc:
        raise RuntimeError(
            "Native NVFP4 extension is missing; rebuild experimental Dockerfile.nvfp4"
        ) from exc
    reason = nvfp4.unavailable_reason()
    if reason:
        raise RuntimeError(f"Native NVFP4 kernel unavailable: {reason}")
