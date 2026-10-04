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

        for block_idx, entries in block_key_map.items():
            names = frozenset(
                make_block_key(self.blocks_prefix, block_idx, param_name)
                for _sft_key, param_name in entries
            )
            block_sd = load_state_dict(
                self.model_path,
                self.model_loader,
                self.registry,
                torch.device("cpu"),
                self._filtered_sd_ops(f"i2v_progressive_block_{block_idx}", names),
            )
            # Avoid success with missing/extra transformed keys; that could
            # otherwise yield an incomplete or visually different model.
            if set(block_sd.sd) != names:
                raise RuntimeError(
                    f"I2V progressive block {block_idx}: checkpoint keys differ "
                    f"from pinned FP8 layout (missing={sorted(names - set(block_sd.sd))}, "
                    f"extra={sorted(set(block_sd.sd) - names)})"
                )
            for key, fused in fuse_lora_weights(
                block_sd, lora_sd_and_strengths,
                fuse_rule=self.fuse_rule, preserve_input_device=False,
            ):
                if key not in names:
                    raise RuntimeError(
                        f"I2V progressive block {block_idx}: unexpected fused key {key}"
                    )
                fill_views[key].copy_(fused, non_blocking=True)
                block_sd.sd[key] = None
            # Preserve original per-block tensor/view iteration order.
            for _sft_key, param_name in entries:
                key = make_block_key(self.blocks_prefix, block_idx, param_name)
                original = block_sd.sd[key]
                if original is not None:
                    fill_views[key].copy_(original)
                    block_sd.sd[key] = None
            # Python loop variables retain their last value after iteration.
            # Dispose the final temporary tensor explicitly as well.
            del original
            # Releasing the per-block raw tensors here is the sole memory
            # optimization; final pinned blocks remain live for ALL forwards.
            del block_sd

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
