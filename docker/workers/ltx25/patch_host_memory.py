"""Apply the bounded CPU-memory fallback to the *pinned* LTX-2 source.

LTX is installed from a fixed commit in the Dockerfile. Apply this small,
strict patch before pip install; fail the Docker build if upstream drifted.
Never silently change I2V's production image (it is a separate Salad group).
"""

from __future__ import annotations

import argparse
from pathlib import Path

UTILS = Path("packages/ltx-core/src/ltx_core/block_streaming/utils.py")
PROVIDER = Path("packages/ltx-core/src/ltx_core/block_streaming/provider.py")


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(
            f"LTX pinned-host patch expected one {label} anchor, found {count}; "
            "review the exact upstream commit instead of applying a blind patch"
        )
    return source.replace(old, new, 1)


def patch_sources(utils_source: str, provider_source: str) -> tuple[str, str]:
    """Return patched source text, requiring the expected upstream anchors."""
    utils_source = _replace_once(
        utils_source,
        "import math\nimport weakref",
        "import logging\nimport math\nimport weakref",
        label="utils imports",
    )
    utils_source = _replace_once(
        utils_source,
        "_BUFFER_ALIGN = 16\n",
        '''_BUFFER_ALIGN = 16

logger = logging.getLogger(__name__)


def _report_host_memory_event(phase: str, **details: int | str) -> None:
    # Do not couple LTX to the factory when used outside our Docker image.
    try:
        from ai_video_factory.inference.gpu_diagnostics import diagnostic_host_memory_event
    except ImportError:
        return
    try:
        diagnostic_host_memory_event(phase, **details)
    except Exception:
        logger.exception("LTX host-memory diagnostics failed")


''',
        label="host-memory diagnostic helper",
    )
    utils_source = _replace_once(
        utils_source,
        (
            "    err = int(cudart.cudaHostRegister(ptr, nbytes, 0))\n"
            "    if err != 0:\n"
            "        return None"
        ),
        '''    err = int(cudart.cudaHostRegister(ptr, nbytes, 0))
    if err != 0:
        logger.warning(
            "LTX_HOST_MEMORY_REGISTER_REJECTED requested_bytes=%d cuda_error_code=%d",
            nbytes, err,
        )
        _report_host_memory_event(
            "ltx.host_memory.register_rejected",
            requested_bytes=nbytes,
            cuda_error_code=err,
        )
        return None''',
        label="CUDA host registration rejection",
    )
    utils_source = _replace_once(
        utils_source,
        '''    if pin_memory and (device is None or torch.device(device).type == "cpu"):
        buf = _alloc_pinned_exact(nbytes)
        if buf is not None:
            return buf
    return torch.empty(nbytes, dtype=torch.uint8, device=device, pin_memory=pin_memory)''',
        '''    if pin_memory and (device is None or torch.device(device).type == "cpu"):
        buf = _alloc_pinned_exact(nbytes)
        if buf is not None:
            return buf
        try:
            return torch.empty(nbytes, dtype=torch.uint8, device=device, pin_memory=True)
        except Exception as exc:
            # CUDA may reject host-pinned allocations while GPU VRAM is empty.
            # Never hide an unrelated failure (e.g. invalid device or bad shape).
            message = str(exc).lower()
            if "out of memory" not in message and "cudaerrormemoryallocation" not in message:
                raise
            logger.warning(
                "LTX_HOST_MEMORY_PAGEABLE_FALLBACK requested_bytes=%d error_type=%s; "
                "using blocking H2D copies for pageable source buffers",
                nbytes, type(exc).__name__,
            )
            _report_host_memory_event(
                "ltx.host_memory.pageable_fallback",
                requested_bytes=nbytes,
                error_type=type(exc).__name__,
            )
            return torch.empty(nbytes, dtype=torch.uint8, device=device, pin_memory=False)
    return torch.empty(nbytes, dtype=torch.uint8, device=device, pin_memory=pin_memory)''',
        label="pinned allocation and OOM fallback",
    )
    provider_source = _replace_once(
        provider_source,
        "raw[:nbytes].copy_(cpu_buffer[:nbytes], non_blocking=self._sync.is_async_copy)",
        '''# Pageable host buffers must never be reused while H2D is in flight.
            # A blocking copy completes before the existing source release.
            raw[:nbytes].copy_(
                cpu_buffer[:nbytes],
                non_blocking=self._sync.is_async_copy and cpu_buffer.is_pinned(),
            )''',
        label="pageable host-to-device transfer safety",
    )
    return utils_source, provider_source


def patch_tree(root: Path) -> None:
    paths = (root / UTILS, root / PROVIDER)
    original = [path.read_text(encoding="utf-8") for path in paths]
    patched = patch_sources(*original)
    for path, result in zip(paths, patched, strict=True):
        # Ensure both files patch cleanly before touching either file.
        path.write_text(result, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch pinned LTX host streaming memory")
    parser.add_argument("--ltx-root", type=Path, required=True)
    args = parser.parse_args()
    patch_tree(args.ltx_root)
    print("LTX pinned host buffer OOM fallback and blocking pageable copies installed")


if __name__ == "__main__":
    main()
