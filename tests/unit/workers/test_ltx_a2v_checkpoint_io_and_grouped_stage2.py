"""CPU-only byte-parity tests for the exact experimental Stage2 loader."""
from __future__ import annotations

import ast
import mmap
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_video_factory.workers.ltx25.checkpoint_io import probe_checkpoint_direct_read


def _patched_helper():
    # Extract the actual code installed into the pinned upstream Docker. No
    # reimplemented copy logic in tests and no CUDA dependency on CI.
    from docker.workers.ltx25.patch_i2v_progressive import patch_builder_source

    source = """
class StreamingModelBuilder:
    @property
    def model_class_configurator(self):
        pass

    def _build_pinned_source(self):
        block_sd = load_state_dict(
            self.model_path,
            self.model_loader,
            self.registry,
            torch.device("cpu"),
            self._filtered_sd_ops("blocks", frozenset(fill_views)),
        )

    def _build_disk_source(
        self,
    ):
        pass
"""
    patched = patch_builder_source(source)
    tree = ast.parse(patched)
    builder = next(x for x in tree.body if isinstance(x, ast.ClassDef))
    helper = next(x for x in builder.body
                  if isinstance(x, ast.FunctionDef)
                  and x.name == "_load_i2v_progressive_pinned_blocks")
    class_type = ast.ClassDef(
        name="Builder", bases=[], keywords=[], body=[helper], decorator_list=[],
    )
    mod = ast.fix_missing_locations(ast.Module(body=[class_type], type_ignores=[]))
    loads: list[set[str]] = []

    class FakeTensor:
        def __init__(self, content: bytes) -> None:
            self.content = content

    class FakeView:
        def __init__(self) -> None:
            self.content = b""

        def copy_(self, tensor: FakeTensor, **_kwargs: object) -> None:
            self.content = tensor.content

    def loader(_path, _loader, _registry, _device, sd_ops):
        names = sd_ops[1]
        loads.append(set(names))
        return SimpleNamespace(sd={
            name: FakeTensor(("original-" + name).encode()) for name in names
        })

    scope = {
        "torch": SimpleNamespace(device=lambda _device: "cpu"),
        "load_state_dict": loader,
        "fuse_lora_weights": lambda *args, **kwargs: (),
        "make_block_key": lambda prefix, i, name: f"{prefix}.{i}.{name}",
    }
    exec(compile(mod, "<exact-upstream-patch>", "exec"), scope)
    return scope["Builder"], FakeView, loads


@pytest.mark.parametrize("group_size,expected_calls", [(1, 9), (2, 5), (4, 3), (8, 2)])
def test_grouped_progressive_loads_the_exact_original_bytes(
    group_size: int, expected_calls: int,
) -> None:
    builder_type, view_type, loads = _patched_helper()
    builder = builder_type()
    builder.registry = SimpleNamespace(_cache_weights=False)
    builder.model_path = "pinned-checkpoint"
    builder.model_loader = object()
    builder.blocks_prefix = "transformer_blocks"
    builder.fuse_rule = object()
    builder._a2v_progressive_block_group_size = group_size
    builder._filtered_sd_ops = lambda suffix, names: (suffix, names)
    blocks = {
        i: [(f"weight.{i}", "weight"), (f"bias.{i}", "bias")]
        for i in range(9)
    }
    views = {
        f"transformer_blocks.{i}.{name}": view_type()
        for i in range(9)
        for name in ("weight", "bias")
    }
    builder._load_i2v_progressive_pinned_blocks(blocks, views, [])
    assert len(loads) == expected_calls
    assert sum(map(len, loads)) == 18
    assert all(views[key].content == ("original-" + key).encode()
               for key in views)


def test_grouped_progressive_refuses_missing_weight() -> None:
    builder_type, view_type, _ = _patched_helper()
    obj = builder_type()
    obj.registry = SimpleNamespace(_cache_weights=False)
    obj._a2v_progressive_block_group_size = 99
    with pytest.raises(ValueError, match="1..8"):
        obj._load_i2v_progressive_pinned_blocks({}, {}, [])


def test_direct_io_probe_uses_distant_aligned_reads_without_cached_fallback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "model.safetensors"
    path.touch()
    original_open = os.open
    calls: list[int] = []
    closed: list[int] = []

    def fake_open(_path: Path, flags: int) -> int:
        assert flags & os.O_DIRECT
        return 42

    def fake_read(fd: int, dest: list[memoryview], offset: int) -> int:
        assert fd == 42 and offset % 4096 == 0
        calls.append(offset)
        return len(dest[0])

    with (
        patch.object(Path, "stat", return_value=SimpleNamespace(st_size=512 * 1048576)),
        patch("os.open", side_effect=fake_open),
        patch("os.close", side_effect=closed.append),
        patch("os.preadv", side_effect=fake_read),
    ):
        result = probe_checkpoint_direct_read(path)
    assert result["direct_io"] is True
    assert result["samples"] == 8
    assert len(set(calls)) == 8
    assert closed == [42]


def test_direct_io_probe_rejects_missing_capability_or_slow_read(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model.safetensors"
    model.touch()
    with (
        patch.object(Path, "stat", return_value=SimpleNamespace(st_size=512 * 1048576)),
        patch("os.open", return_value=77),
        patch("os.close"),
        patch("os.preadv", side_effect=OSError("unsupported direct I/O")),
    ):
        with pytest.raises(RuntimeError, match="refusing to rely on cached"):
            probe_checkpoint_direct_read(model, minimum_mibps=500)
