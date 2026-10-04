"""CPU-only equivalence and peak-lifetime gates for the experimental I2V RAM fix."""

from __future__ import annotations

import ast
import gc
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from docker.workers.ltx25.patch_i2v_progressive import (
    patch_builder_source,
    patch_tree,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def upstream_builder_snippet() -> str:
    # Exact required source anchors from pinned LTX; CI also checks the full
    # upstream revision rather than trusting only this deliberately small mock.
    return '''class StreamingModelBuilder:
    @property
    def model_class_configurator(self):
        return None

    def _build_pinned_source(self, blocks, dtype, cpu_slots_count,
                             block_key_map, lora_sd_and_strengths):
        pinned_buffers = {}
        block_layouts = {}
        fill_views = {}
        block_sd = load_state_dict(
            self.model_path,
            self.model_loader,
            self.registry,
            torch.device("cpu"),
            self._filtered_sd_ops("blocks", frozenset(fill_views)),
        )
        pinned = {}
        return PinnedWeightSource(pinned), []

    def _build_disk_source(
        self, blocks, dtype):
        pass
'''


def _helper(code: str, env: dict[str, Any]) -> Any:
    tree = ast.parse(code)
    node = next(
        item for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef)
        and item.name == "_load_i2v_progressive_pinned_blocks"
    )
    exec(compile("from __future__ import annotations\n" + ast.unparse(node),
                 "<patched-pinned-builder>", "exec"), env)
    return env[node.name]


class _Buffer:
    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload

    def copy_(self, other: _Buffer, non_blocking: bool = False) -> None:
        # Output comparison is byte-for-byte, including already-quantized FP8.
        self.payload = other.payload


def _run_progressive(
    code: str, weights: dict[str, bytes], *, missing: bool = False,
    cached: bool = False, loras: bool = False,
) -> tuple[dict[str, bytes], int, list[frozenset[str]], list[weakref.ReferenceType]]:
    sources: list[weakref.ReferenceType] = []
    loaded_by_block: list[frozenset[str]] = []
    max_live = 0

    def alive() -> int:
        return sum(ref() is not None for ref in sources)

    def loader(_path: Any, _loader: Any, _registry: Any, _device: Any,
               allowed: frozenset[str]) -> Any:
        nonlocal max_live
        loaded_by_block.append(allowed)
        data = {
            key: _Buffer(value)
            for key, value in weights.items()
            if key in allowed and (not missing or key != "transformer_blocks.1.b")
        }
        sources.extend(weakref.ref(value) for value in data.values())
        max_live = max(max_live, alive())
        return SimpleNamespace(sd=data)

    environment: dict[str, Any] = {
        "load_state_dict": loader,
        "torch": SimpleNamespace(device=lambda value: value),
        "make_block_key": lambda prefix, idx, name: f"{prefix}.{idx}.{name}",
        "fuse_lora_weights": lambda *_args, **_kw: iter(()),
    }
    helper = _helper(code, environment)
    builder = SimpleNamespace(
        registry=SimpleNamespace(_cache_weights=cached),
        model_path="checkpoint.safetensors",
        model_loader=object(),
        blocks_prefix="transformer_blocks",
        fuse_rule=object(),
        _filtered_sd_ops=lambda _name, allowed: allowed,
    )
    layout = {
        0: [("key0a", "a"), ("key0b", "b")],
        1: [("key1a", "a"), ("key1b", "b")],
    }
    targets = {key: _Buffer() for key in weights}
    if missing:
        with pytest.raises(RuntimeError, match="checkpoint keys differ"):
            helper(builder, layout, targets, [])
    elif cached:
        with pytest.raises(RuntimeError, match="uncached"):
            helper(builder, layout, targets, [])
    elif loras:
        with pytest.raises(RuntimeError, match="no-LoRA"):
            helper(builder, layout, targets, [object()])
    else:
        helper(builder, layout, targets, [])
    gc.collect()
    return (
        {key: view.payload for key, view in targets.items()},
        max_live, loaded_by_block, sources,
    )


def test_progressive_pinned_weights_byte_identical_and_one_raw_block_at_a_time(
    upstream_builder_snippet: str,
) -> None:
    patched = patch_builder_source(upstream_builder_snippet)
    weights = {
        "transformer_blocks.0.a": bytes.fromhex("0001807f"),
        "transformer_blocks.0.b": bytes.fromhex("fe03a4ff"),
        "transformer_blocks.1.a": bytes.fromhex("81fa1234"),
        "transformer_blocks.1.b": bytes.fromhex("09991188"),
    }
    loaded, peak, groups, sources = _run_progressive(patched, weights)
    assert loaded == weights  # exact binary values (not merely close)
    assert peak <= 2  # only the one block's two transient raw tensors
    assert all(ref() is None for ref in sources)
    assert groups == [
        frozenset({"transformer_blocks.0.a", "transformer_blocks.0.b"}),
        frozenset({"transformer_blocks.1.a", "transformer_blocks.1.b"}),
    ]
    assert "if getattr(self, \"_i2v_progressive_pinned_blocks\", False):" in patched
    assert "self._load_i2v_progressive_pinned_blocks(" in patched
    assert 'self._filtered_sd_ops("blocks", frozenset(fill_views))' in patched
    assert "_i2v_progressive_pinned_blocks_supported = True" in patched


@pytest.mark.parametrize("failure", ["missing", "cached", "loras"])
def test_progressive_loader_rejects_unsafe_conditions(
    upstream_builder_snippet: str, failure: str,
) -> None:
    patched = patch_builder_source(upstream_builder_snippet)
    weights = {
        f"transformer_blocks.{idx}.{key}": b"weight"
        for idx in range(2) for key in ("a", "b")
    }
    _run_progressive(
        patched, weights, missing=failure == "missing",
        cached=failure == "cached", loras=failure == "loras",
    )


def test_upstream_patch_is_exact_rejects_drift_and_does_not_change_default(
    upstream_builder_snippet: str, tmp_path: Path,
) -> None:
    patched = patch_builder_source(upstream_builder_snippet)
    ast.parse(patched)
    with pytest.raises(RuntimeError, match="pinned"):
        patch_builder_source(patched)
    with pytest.raises(RuntimeError, match="pinned"):
        patch_builder_source(upstream_builder_snippet.replace(
            "def _build_disk_source(", "def disk_mode(",
        ))
    root = tmp_path / "ltx"
    destination = root / "packages/ltx-core/src/ltx_core/block_streaming/builder.py"
    destination.parent.mkdir(parents=True)
    destination.write_text(upstream_builder_snippet, encoding="utf-8")
    patch_tree(root)
    assert destination.read_text(encoding="utf-8") == patched


def test_i2v_only_opt_in_and_offline_image_gate() -> None:
    model = (
        ROOT / "src/ai_video_factory/workers/ltx25/model.py"
    ).read_text(encoding="utf-8")
    i2v = model[model.index("    def _full_hd_streaming_stage2("):
                model.index("    def _pair_attempt(")]
    assert 'if mode == "cpu":' in i2v
    assert "builder._i2v_progressive_pinned_blocks = True" in i2v
    assert "builder.registry" in i2v
    probe = (
        ROOT / "scripts/smoke/verify_ltx25_i2v_hybrid_image.py"
    ).read_text(encoding="utf-8")
    assert "_i2v_progressive_pinned_blocks_supported" in probe
    assert "_load_i2v_progressive_pinned_blocks" in probe
    assert '_i2v_progressive_pinned_blocks' in probe
    docker = (
        ROOT / "docker/workers/ltx25/Dockerfile"
    ).read_text(encoding="utf-8")
    assert "patch-ltx-i2v-progressive" in docker
