"""Opt-in progressive CPU block initialization for Full HD experimental I2V only.

The Dockerfile pins upstream LTX. Keep its existing _build_pinned_source intact
for every other model/mode. Only the lazy CPU stage2 explicitly opts into this
extra path. Fail the image build if an upstream source anchor changes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

BUILDER = Path("packages/ltx-core/src/ltx_core/block_streaming/builder.py")


def _replace_once(source: str, original: str, replacement: str, label: str) -> str:
    count = source.count(original)
    if count != 1:
        raise RuntimeError(
            f"pinned LTX progressive I2V patch: expected one {label} anchor, "
            f"found {count}; inspect pinned upstream before building"
        )
    return source.replace(original, replacement, 1)


def patch_builder_source(source: str) -> str:
    if "_i2v_progressive_pinned_blocks_supported = True" in source:
        raise RuntimeError("pinned LTX progressive I2V patch already installed")
    # Immutable builder copies made during FP8 policy preparation inherit both
    # this class marker and the opt-in flag on their copied instance.
    source = _replace_once(
        source,
        "    @property\n    def model_class_configurator(self)",
        "    _i2v_progressive_pinned_blocks_supported = True\n\n"
        "    @property\n    def model_class_configurator(self)",
        "explicit patched builder capability",
    )

    source = _replace_once(
        source,
        '''        block_sd = load_state_dict(
            self.model_path,
            self.model_loader,
            self.registry,
            torch.device("cpu"),
            self._filtered_sd_ops("blocks", frozenset(fill_views)),
        )''',
        '''        if getattr(self, "_i2v_progressive_pinned_blocks", False):
            # All persistent CPU buffers already exist, as in the upstream
            # method. Avoid simultaneously materializing ALL raw checkpoint
            # blocks while filling them: hold only one raw block at a time.
            # The downstream pinned source and GPU forward remain identical.
            self._load_i2v_progressive_pinned_blocks(
                block_key_map, fill_views, lora_sd_and_strengths,
            )
            pinned = {
                idx: PinnedBlock(pinned_buffers[idx], block_layouts[idx])
                for idx in pinned_buffers
            }
            return PinnedWeightSource(pinned), []

        block_sd = load_state_dict(
            self.model_path,
            self.model_loader,
            self.registry,
            torch.device("cpu"),
            self._filtered_sd_ops("blocks", frozenset(fill_views)),
        )''',
        "opt-in progressive branch within pinned source",
    )

    source = _replace_once(
        source,
        '''    def _build_disk_source(
        self,''',
        '''    def _load_i2v_progressive_pinned_blocks(
        self,
        block_key_map: dict[int, list[tuple[str, str]]],
        fill_views: dict[str, torch.Tensor],
        lora_sd_and_strengths: list[LoraStateDictWithStrength],
    ) -> None:
        """Fill exact same pinned views, materializing one raw block at a time.

        Each block uses upstream load_state_dict, original SD operations and
        original FP8 fusion/copy sequence. No math, dtype, kernel, block
        residency or H2D policy changes after stage2 has been constructed.
        """
        # The standard lazy stage uses a non-weight-caching registry. Caching
        # whole transient blocks would defeat the memory bound.
        if getattr(self.registry, "_cache_weights", None) is not False:
            raise RuntimeError("I2V progressive stage2 requires an uncached weight registry")
        if lora_sd_and_strengths:
            raise RuntimeError(
                "I2V progressive stage2 permits only the approved no-LoRA FP8 recipe"
            )

        # A2V experimental groups nearby blocks to amortize repeated
        # safetensors metadata/setup and improve sequential checkpoint I/O.
        # I2V continues using the previous one-block-at-a-time default.
        group_size = getattr(self, "_a2v_progressive_block_group_size", 1)
        if not isinstance(group_size, int) or not 1 <= group_size <= 8:
            raise ValueError("A2V progressive group size must be 1..8")
        block_items = sorted(block_key_map.items())
        for start in range(0, len(block_items), group_size):
            group = block_items[start:start + group_size]
            names = frozenset(
                make_block_key(self.blocks_prefix, block_idx, param_name)
                for block_idx, entries in group
                for _sft_key, param_name in entries
            )
            block_sd = load_state_dict(
                self.model_path,
                self.model_loader,
                self.registry,
                torch.device("cpu"),
                self._filtered_sd_ops(
                    f"progressive_blocks_{group[0][0]}_{group[-1][0]}", names,
                ),
            )
            # Never silently omit, reorder or numerically transform a weight.
            if set(block_sd.sd) != names:
                raise RuntimeError(
                    f"Progressive blocks {group[0][0]}-{group[-1][0]}: "
                    f"checkpoint keys differ from original pinned FP8 layout "
                    f"(missing={sorted(names - set(block_sd.sd))}, "
                    f"extra={sorted(set(block_sd.sd) - names)})"
                )
            for key, fused in fuse_lora_weights(
                block_sd, lora_sd_and_strengths,
                fuse_rule=self.fuse_rule, preserve_input_device=False,
            ):
                if key not in names:
                    raise RuntimeError(f"Unexpected progressive fused key {key}")
                fill_views[key].copy_(fused, non_blocking=True)
                block_sd.sd[key] = None
            # Every destination view receives the original checkpoint tensor
            # exactly once. No weights are retained after this group is filled.
            for block_idx, entries in group:
                for _sft_key, param_name in entries:
                    key = make_block_key(self.blocks_prefix, block_idx, param_name)
                    original = block_sd.sd[key]
                    if original is not None:
                        fill_views[key].copy_(original)
                        block_sd.sd[key] = None
            del original, block_sd

    def _build_disk_source(
        self,''',
        "new progressive-only builder helper",
    )
    return source


def patch_tree(root: Path) -> None:
    path = root / BUILDER
    original = path.read_text(encoding="utf-8")
    patched = patch_builder_source(original)
    path.write_text(patched, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Opt-in pinned LTX I2V progressive stage2")
    parser.add_argument("--ltx-root", type=Path, required=True)
    args = parser.parse_args()
    patch_tree(args.ltx_root)
    print("LTX I2V stage2 progressive CPU block loader installed (default OFF)")


if __name__ == "__main__":
    main()
