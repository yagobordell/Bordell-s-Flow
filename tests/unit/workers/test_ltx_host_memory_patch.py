"""Offline behavioral tests for the Docker-patched, immutable upstream LTX source."""

from __future__ import annotations

import ast
import logging
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from docker.workers.ltx25.patch_host_memory import patch_sources, patch_tree


@pytest.fixture
def upstream_snippets() -> tuple[str, str]:
    utils = '''import math
import weakref

_BUFFER_ALIGN = 16


def _alloc_pinned_exact(nbytes):
    err = int(cudart.cudaHostRegister(ptr, nbytes, 0))
    if err != 0:
        return None


def alloc_buffer(nbytes: int, device: object, pin_memory: bool):
    if pin_memory and not torch.cuda.is_available():
        pin_memory = False
    if pin_memory and (device is None or torch.device(device).type == "cpu"):
        buf = _alloc_pinned_exact(nbytes)
        if buf is not None:
            return buf
    return torch.empty(nbytes, dtype=torch.uint8, device=device, pin_memory=pin_memory)
'''
    provider = '''class WeightsProvider:
    def _copy_to_gpu(self, idx, raw, gpu_weights, cpu_buffer, nbytes):
        if (
            not cpu_buffer.is_contiguous()
            or cpu_buffer.dtype != torch.uint8
            or cpu_buffer.numel() < nbytes
        ):
            raise ValueError("invalid source buffer")
        with self._sync.copy_scope():
            raw[:nbytes].copy_(cpu_buffer[:nbytes], non_blocking=self._sync.is_async_copy)
            if self._lora_sources:
                self._fuse_block_loras(idx, gpu_weights)
        return self._sync.commit_copy()
'''
    return utils, provider


def _load_function(source: str, name: str, namespace: dict[str, Any]) -> Any:
    parsed = ast.parse(source)
    node = next(
        node for node in ast.walk(parsed)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    # Only execute the chosen upstream function, never import GPU dependencies.
    snippet = "from __future__ import annotations\n" + ast.unparse(node)
    exec(compile(snippet, "<patched-ltx>", "exec"), namespace)
    return namespace[name]


def test_cuda_host_pinning_oom_uses_normal_cpu_buffer(
    upstream_snippets: tuple[str, str],
) -> None:
    utils, provider = patch_sources(*upstream_snippets)
    allocs: list[bool] = []
    events: list[tuple[str, dict[str, int | str]]] = []

    class PinnedOom(RuntimeError):
        pass

    def empty(nbytes: int, *, dtype: str, device: object, pin_memory: bool) -> object:
        assert nbytes == 900_000_000 and dtype == "uint8" and device == "cpu"
        allocs.append(pin_memory)
        if pin_memory:
            raise PinnedOom("CUDA error: out of memory")
        return SimpleNamespace(is_pinned=lambda: False)

    torch = SimpleNamespace(
        uint8="uint8",
        cuda=SimpleNamespace(is_available=lambda: True),
        device=lambda value: SimpleNamespace(type=value),
        empty=empty,
    )
    alloc = _load_function(utils, "alloc_buffer", {
        "torch": torch,
        "_alloc_pinned_exact": lambda nbytes: None,
        "logger": logging.getLogger(__name__),
        "_report_host_memory_event": lambda phase, **details: events.append((phase, details)),
    })
    normal = alloc(900_000_000, "cpu", True)
    assert normal.is_pinned() is False
    assert allocs == [True, False]
    assert events == [(
        "ltx.host_memory.pageable_fallback",
        {"requested_bytes": 900_000_000, "error_type": "PinnedOom"},
    )]
    assert "cpu_buffer.is_pinned()" in provider


def test_non_memory_cuda_errors_are_never_suppressed(
    upstream_snippets: tuple[str, str],
) -> None:
    utils, _ = patch_sources(*upstream_snippets)

    def fail(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("CUDA error: invalid device")

    torch = SimpleNamespace(
        uint8="uint8",
        cuda=SimpleNamespace(is_available=lambda: True),
        device=lambda value: SimpleNamespace(type=value),
        empty=fail,
    )
    alloc = _load_function(utils, "alloc_buffer", {
        "torch": torch,
        "_alloc_pinned_exact": lambda nbytes: None,
    })
    with pytest.raises(RuntimeError, match="invalid device"):
        alloc(100, "cpu", True)


@pytest.mark.parametrize("pinned,expected_non_blocking", [(True, True), (False, False)])
def test_pageable_gpu_copy_is_blocking_before_source_reuse(
    upstream_snippets: tuple[str, str], pinned: bool, expected_non_blocking: bool
) -> None:
    _, provider = patch_sources(*upstream_snippets)
    flags: list[bool] = []

    class Buffer:
        dtype = "uint8"

        def __getitem__(self, index: slice) -> Buffer:
            del index
            return self

        def is_contiguous(self) -> bool:
            return True

        def numel(self) -> int:
            return 100

        def is_pinned(self) -> bool:
            return pinned

        def copy_(self, other: Buffer, *, non_blocking: bool) -> None:
            assert other is cpu
            flags.append(non_blocking)

    cpu = Buffer()
    gpu = Buffer()
    sync = SimpleNamespace(
        is_async_copy=True, copy_scope=nullcontext,
        commit_copy=lambda: "copy-finished-event",
    )
    copy_to_gpu = _load_function(
        provider, "_copy_to_gpu", {"torch": SimpleNamespace(uint8="uint8")},
    )
    service = SimpleNamespace(_sync=sync, _lora_sources=[])
    assert copy_to_gpu(service, 0, gpu, {}, cpu, 100) == "copy-finished-event"
    assert flags == [expected_non_blocking]


def test_pinned_allocation_success_remains_unchanged(
    upstream_snippets: tuple[str, str],
) -> None:
    utils, _ = patch_sources(*upstream_snippets)
    pinned = object()
    torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: True),
        device=lambda value: SimpleNamespace(type=value),
        empty=lambda *args, **kwargs: pytest.fail("must not allocate twice"),
    )
    alloc = _load_function(utils, "alloc_buffer", {
        "torch": torch,
        "_alloc_pinned_exact": lambda nbytes: pinned,
    })
    assert alloc(100, "cpu", True) is pinned


def test_patch_is_strict_and_build_fails_on_upstream_drift(
    tmp_path: Path, upstream_snippets: tuple[str, str],
) -> None:
    utils, provider = upstream_snippets
    with pytest.raises(RuntimeError, match="upstream commit"):
        patch_sources(utils.replace("import weakref", "import elsewhere"), provider)
    root = tmp_path / "ltx"
    util_path = root / "packages/ltx-core/src/ltx_core/block_streaming/utils.py"
    provider_path = root / "packages/ltx-core/src/ltx_core/block_streaming/provider.py"
    util_path.parent.mkdir(parents=True)
    util_path.write_text(utils, encoding="utf-8")
    provider_path.write_text(provider, encoding="utf-8")
    patch_tree(root)
    assert "LTX_HOST_MEMORY_PAGEABLE_FALLBACK" in util_path.read_text(encoding="utf-8")
    assert "cpu_buffer.is_pinned()" in provider_path.read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="upstream commit"):
        patch_tree(root)
