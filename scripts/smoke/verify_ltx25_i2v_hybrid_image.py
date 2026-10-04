"""Fail closed BEFORE Salad startup if a pinned Docker lacks hybrid Full HD I2V.

The pinned image can have the right repository/digest syntax and still be
the previous all-DISK build. Inspect its ACTUAL installed code offline, with
no GPU or Salad API access. Docker may pull the image locally if absent:
that costs bandwidth/time, but avoids a billed 10-30 minute Salad bootstrap.
"""

from __future__ import annotations

import argparse
import re
import subprocess

_PINNED = re.compile(r"[^\s@]+@sha256:[a-fA-F0-9]{64}\Z")

# The assertion executes INSIDE the image, not in the host checkout.
# Validate all interfaces needed by the hybrid flow, not merely the field
# whose absence caused a previous paid job to fail during task validation.
_IMAGE_PROBE = """
import ast
import inspect
import textwrap
from ai_video_factory.workers.ltx25.experimental_batch2 import Batch2Parameters
from ai_video_factory.workers.ltx25.paired_i2v import STAGE2_SIGMAS_SAFE_ABI
from ai_video_factory.workers.ltx25.model import DirectLTX25Backend
from ai_video_factory.workers.ltx25.paired_i2v import generate_paired_fast_distilled
from ltx_core.block_streaming.builder import StreamingModelBuilder
from ai_video_factory.workers.ltx25.shared_i2v_decoder import SharedSequentialVideoDecoder
from ltx_core.model.video_vae.transformer.attention import natten_available

assert "full_hd_stage2_mode" in Batch2Parameters.model_fields, "old batch2 request schema"
assert "full_hd_stage2_mode" in inspect.signature(
    DirectLTX25Backend.generate_pair
).parameters, "old backend call contract"
assert hasattr(DirectLTX25Backend, "_full_hd_streaming_stage2"), "no hybrid streaming stage"
assert "stage2_factory" in inspect.signature(
    generate_paired_fast_distilled
).parameters, "old nonhybrid paired pipeline"
assert STAGE2_SIGMAS_SAFE_ABI == "stage2-sigma-separated-v1", (
    "old hybrid image: stage2 DiffusionStage may shadow its sigma tensor"
)
# Verify executable source inside this immutable image, not local checkout
# signatures or a marker alone. This guards the exact bug behind the failed
# October 4 Full-HD trial, before a billed remote start.
tree = ast.parse(textwrap.dedent(inspect.getsource(generate_paired_fast_distilled)))
paired_calls = [
    node for node in ast.walk(tree)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Name)
    and node.func.id == "_paired_stage"
]
def has_keyword(node, name, value):
    return any(
        item.arg == name and isinstance(item.value, ast.Name)
        and item.value.id == value for item in node.keywords
    )
def is_second_stage(node):
    return any(
        item.arg == "stage_label"
        and isinstance(item.value, ast.Constant)
        and item.value.value == "stage2"
        for item in node.keywords
    )
assert any(
    is_second_stage(node) and has_keyword(node, "sigmas", "stage2_sigmas")
    for node in paired_calls
), "old hybrid image: stage2 must consume independent sigma tensor"
# The image must contain the progressive per-block loader, and the factory
# must activate it ONLY for the CPU alternative. An older working hybrid
# Docker is not the same memory-optimized image.
assert getattr(
    StreamingModelBuilder, "_i2v_progressive_pinned_blocks_supported", False
), "image lacks progressive I2V upstream builder patch"
assert hasattr(
    StreamingModelBuilder, "_load_i2v_progressive_pinned_blocks"
), "image lacks progressive I2V loader implementation"
builder_source = inspect.getsource(StreamingModelBuilder._build_pinned_source)
assert 'getattr(self, "_i2v_progressive_pinned_blocks", False)' in builder_source, (
    "image does not gate progressive loader behind opt-in"
)
stage2_source = inspect.getsource(DirectLTX25Backend._full_hd_streaming_stage2)
assert 'builder._i2v_progressive_pinned_blocks = True' in stage2_source, (
    "I2V hybrid factory does not enable progressive stage2"
)
# Verify the installed exact image contains the shared VAE implementation
# and that only Full HD opts into it. The pinned runtime intentionally forces
# eager SDPA after an RTX 5090 NATTEN native segfault; do not silently change
# that attention algorithm to claim a speedup.
source = inspect.getsource(DirectLTX25Backend._pair_attempt)
assert 'SharedSequentialVideoDecoder(original_decoder)' in source, (
    "image lacks the isolated Full HD shared VAE encoder"
)
assert 'if is_full_hd else nullcontext()' in source, (
    "image must retain the unchanged 720p decode path"
)
assert hasattr(SharedSequentialVideoDecoder, "close"), (
    "image lacks shared VAE exception cleanup"
)
from ai_video_factory.workers.ltx25.model import _select_diffvae_backend
assert '_force_diffvae_eager_sdpa' in inspect.getsource(_select_diffvae_backend), (
    "image changed the safety-pinned eager decode backend"
)
print("I2V_VAE_RUNTIME_STATUS natten_importable=" + str(natten_available()) +
      " selected_baseline=eager_sdpa (prior RTX5090 native NATTEN crash)")
print("I2V_HYBRID_DOCKER_ABI_OK")
"""


_TRITON_IMAGE_PROBE = """
import triton
from ltx_core.model.video_vae.transformer.fallback_na.triton_na import na3d
from ai_video_factory.workers.ltx25.triton_na_probe import ensure_triton_gpu_probe
from ai_video_factory.workers.ltx25.model import _select_diffvae_backend, DirectLTX25Backend
from ai_video_factory.workers.ltx25.experimental_batch2 import Batch2Parameters
assert callable(na3d) and callable(ensure_triton_gpu_probe)
assert "vae_backend" in Batch2Parameters.model_fields
assert "triton_strict" in inspect.getsource(_select_diffvae_backend)
assert "ensure_triton_gpu_probe()" in inspect.getsource(DirectLTX25Backend.generate_pair)
assert "vae_backend" in inspect.signature(DirectLTX25Backend.generate_pair).parameters
print("I2V_TRITON_DOCKER_ABI_OK version=" + triton.__version__)
"""


def verify(image: str, *, require_triton: bool = False) -> None:
    if _PINNED.fullmatch(image) is None:
        raise ValueError("Expected immutable Docker repository@sha256 digest")
    argv = [
        "docker", "run", "--rm", "--network", "none",
        "--entrypoint", "python", image, "-c",
        _IMAGE_PROBE + (_TRITON_IMAGE_PROBE if require_triton else ""),
    ]
    try:
        result = subprocess.run(
            argv, check=False, text=True, capture_output=True, timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Could not inspect the exact pinned Docker image locally; "
            "NO Salad GPU must be started"
        ) from exc
    if (
        result.returncode != 0
        or "I2V_HYBRID_DOCKER_ABI_OK" not in result.stdout
        or (require_triton and "I2V_TRITON_DOCKER_ABI_OK" not in result.stdout)
    ):
        details = (result.stderr or result.stdout).strip()[-1800:]
        raise RuntimeError(
            "Pinned Docker does NOT prove hybrid I2V compatibility. "
            "Rebuild the image from the reviewed hybrid branch, publish, "
            f"then Prepare the STOPPED I2V group with its NEW digest. Details: {details}"
        )
    print(f"I2V_HYBRID_DOCKER_ABI_OK pinned_image={image}")
    if require_triton:
        print(f"I2V_TRITON_DOCKER_ABI_OK pinned_image={image}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--require-triton", action="store_true")
    args = parser.parse_args()
    verify(args.image, require_triton=args.require_triton)


if __name__ == "__main__":
    main()
