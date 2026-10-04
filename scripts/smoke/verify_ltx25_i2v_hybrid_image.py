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
print("I2V_HYBRID_DOCKER_ABI_OK")
"""


def verify(image: str) -> None:
    if _PINNED.fullmatch(image) is None:
        raise ValueError("Expected immutable Docker repository@sha256 digest")
    argv = [
        "docker", "run", "--rm", "--network", "none",
        "--entrypoint", "python", image, "-c", _IMAGE_PROBE,
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
    if result.returncode != 0 or "I2V_HYBRID_DOCKER_ABI_OK" not in result.stdout:
        details = (result.stderr or result.stdout).strip()[-1800:]
        raise RuntimeError(
            "Pinned Docker does NOT prove hybrid I2V compatibility. "
            "Rebuild the image from the reviewed hybrid branch, publish, "
            f"then Prepare the STOPPED I2V group with its NEW digest. Details: {details}"
        )
    print(f"I2V_HYBRID_DOCKER_ABI_OK pinned_image={image}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    verify(args.image)


if __name__ == "__main__":
    main()
