"""Isolated GPU preflight for the opt-in 1080p A2V Triton DiffVAE experiment.

Run in a CHILD process: native CUDA kernel faults must never crash the
persistent worker or leave it pretending the backend was validated. This
compares upstream pinned LTX Triton NA directly with its unchanged eager
SDPA reference. Passing this tiny numerical gate is NOT video quality
approval; matching full-length baseline videos remain mandatory.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

_OK = "A2V_TRITON_NA_GPU_PROBE_OK"
_TIMEOUT_SECONDS = 180


def _gpu_probe() -> None:
    import torch  # noqa: PLC0415
    from ltx_core.model.video_vae.transformer.fallback_na.eager import (  # noqa: PLC0415
        na3d as eager_na3d,
    )
    from ltx_core.model.video_vae.transformer.fallback_na.triton_na import (  # noqa: PLC0415
        na3d as triton_na3d,
    )

    if not torch.cuda.is_available():
        raise RuntimeError("Triton A2V preflight needs a real CUDA GPU")
    torch.manual_seed(716)
    results = []
    # Boundary-shifted and interior windows; production NA head dimension 64.
    # Small shapes keep the independent probe cheap and avoid full video weights.
    for t, h, w, kernel in (
        (3, 9, 11, (3, 7, 7)),
        (4, 7, 9, (3, 5, 5)),
        (3, 5, 7, (3, 3, 3)),
    ):
        shape = (1, t, h, w, 2, 64)
        q, k, v = (
            torch.randn(shape, device="cuda", dtype=torch.bfloat16).contiguous()
            for _ in range(3)
        )
        eager = eager_na3d(q, k, v, kernel, scale=1.0)
        # Synchronize so asynchronous device faults become errors HERE.
        torch.cuda.synchronize()
        triton = triton_na3d(q, k, v, kernel, scale=1.0)
        torch.cuda.synchronize()
        diff = (triton.float() - eager.float()).abs()
        if not torch.isfinite(triton).all():
            raise RuntimeError(f"Nonfinite Triton output for shape={shape}")
        # BF16 kernels differ by roundoff, but a different boundary/window
        # convention or substantial output drift must fail before inference.
        torch.testing.assert_close(triton, eager, atol=0.04, rtol=0.04)
        results.append({
            "shape": list(shape),
            "kernel": list(kernel),
            "max_abs_error": float(diff.max().item()),
            "mean_abs_error": float(diff.mean().item()),
        })
    print(_OK + " " + json.dumps({"results": results}, sort_keys=True), flush=True)


def ensure_triton_gpu_probe() -> float:
    """Return isolated GPU probe wall time; NEVER silently fall back to eager."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-m", __name__, "--gpu-probe"],
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Experimental A2V Triton GPU preflight unavailable; no inference submitted"
        ) from exc
    if result.returncode != 0 or _OK not in result.stdout:
        raise RuntimeError(
            f"Experimental A2V Triton GPU preflight FAILED "
            f"(exit={result.returncode}): {(result.stderr or result.stdout)[-1400:]}"
        )
    elapsed = time.monotonic() - start
    logger.info(
        "LTX25_A2V_TRITON_PREFLIGHT_OK seconds=%.3f results=%s",
        elapsed, result.stdout.strip(),
    )
    return elapsed


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args != ["--gpu-probe"]:
        raise SystemExit("Only --gpu-probe may execute the isolated CUDA test")
    _gpu_probe()


if __name__ == "__main__":
    main()
