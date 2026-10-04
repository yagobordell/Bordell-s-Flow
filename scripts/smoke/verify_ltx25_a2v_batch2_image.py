"""Free exact-digest Docker ABI gate before allocating Salad GPU for A2V B2."""

from __future__ import annotations

import argparse
import re
import subprocess

_IMAGE_PROBE = """
import inspect
import torch
from ltx_pipelines.utils.blocks import DiffusionStage, _build_state
from ltx_pipelines.utils.samplers import euler_ancestral_denoising_loop
from ai_video_factory.workers.ltx25.a2v import DirectLTX25AudioToVideoBackend
from ai_video_factory.workers.ltx25.paired_a2v import generate_paired_static_a2v
from ai_video_factory.workers.ltx25.experimental_a2v_batch2 import (
    A2VBatch2Parameters, ExperimentalA2VBatch2TaskRunner,
)
from ai_video_factory.workers.ltx25.shared_a2v_decoder import SharedA2VDecoder
from ai_video_factory.workers.ltx25.triton_a2v_probe import ensure_triton_gpu_probe
from ltx_core.block_streaming.builder import StreamingModelBuilder
from ai_video_factory.workers.ltx25.checkpoint_io import probe_checkpoint_direct_read
assert callable(_build_state) and callable(generate_paired_static_a2v)
assert callable(euler_ancestral_denoising_loop)
assert "new_noise_fn" in inspect.signature(euler_ancestral_denoising_loop).parameters
assert "_i2v_progressive_pinned_blocks_supported" in inspect.getsource(StreamingModelBuilder)
assert "_a2v_progressive_block_group_size" in inspect.getsource(
    StreamingModelBuilder._load_i2v_progressive_pinned_blocks
)
assert callable(probe_checkpoint_direct_read)
assert "generate_pair" in dir(DirectLTX25AudioToVideoBackend)
assert "actual_gpu_batch_size" in inspect.getsource(DirectLTX25AudioToVideoBackend.generate_pair)
assert "stage2_factory" in inspect.signature(generate_paired_static_a2v).parameters
assert "max_transformer_forward_batch_size" in inspect.signature(
    generate_paired_static_a2v
).parameters
assert "max_stage1_major_page_faults" in inspect.signature(
    generate_paired_static_a2v
).parameters
assert "max_stage1_major_page_faults" in inspect.signature(
    DirectLTX25AudioToVideoBackend.generate_pair
).parameters
for key in (
    "progressive_block_group_size", "min_checkpoint_direct_read_mibps",
    "max_stage1_build_seconds",
):
    assert key in inspect.signature(DirectLTX25AudioToVideoBackend.generate_pair).parameters
assert hasattr(SharedA2VDecoder, "close")
assert A2VBatch2Parameters.model_fields["stage2_mode"].default == "cpu"
assert A2VBatch2Parameters.model_fields["vae_backend"].default == "eager"
assert A2VBatch2Parameters.model_fields["guidance_forward_batch_size"].default == 2
assert A2VBatch2Parameters.model_fields["max_stage1_major_page_faults"].default == 0
assert A2VBatch2Parameters.model_fields["progressive_block_group_size"].default == 1
assert A2VBatch2Parameters.model_fields["min_checkpoint_direct_read_mibps"].default == 0.0
assert A2VBatch2Parameters.model_fields["max_stage1_build_seconds"].default == 0.0
assert callable(ensure_triton_gpu_probe)
assert "requested_pair_vae_backend" in inspect.signature(
    DirectLTX25AudioToVideoBackend._get_or_build_pipeline
).parameters
assert "_prepare_independent_prompt_contexts" in inspect.getsource(
    generate_paired_static_a2v
)
assert ExperimentalA2VBatch2TaskRunner.task_name.endswith("batch2_experimental")
print("LTX25_A2V_B2_DOCKER_ABI_OK torch=" + torch.__version__, flush=True)
"""


_TRITON_IMAGE_PROBE = """
import triton
from ltx_core.model.video_vae.transformer.fallback_na.triton_na import na3d
from ai_video_factory.workers.ltx25.triton_a2v_probe import ensure_triton_gpu_probe
assert callable(na3d) and callable(ensure_triton_gpu_probe)
print("LTX25_A2V_B2_TRITON_IMAGE_OK", flush=True)
"""


def verify(image: str, *, require_triton: bool = False) -> None:
    repo, separator, digest = image.partition("@sha256:")
    if (
        not separator or not repo or any(char.isspace() for char in repo)
        or "@" in repo or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
    ):
        raise ValueError("Only an immutable digest-pinned Docker image is accepted")
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", _IMAGE_PROBE],
        check=False, text=True, capture_output=True, timeout=180,
    )
    if result.returncode != 0 or "LTX25_A2V_B2_DOCKER_ABI_OK" not in result.stdout:
        raise RuntimeError(
            "A2V B2 Docker ABI verification FAILED; never start paid Salad: "
            + (result.stderr or result.stdout)[-1800:]
        )
    print(result.stdout.strip())
    if require_triton:
        strict = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "python",
             image, "-c", _TRITON_IMAGE_PROBE],
            check=False, text=True, capture_output=True, timeout=180,
        )
        if strict.returncode != 0 or "LTX25_A2V_B2_TRITON_IMAGE_OK" not in strict.stdout:
            raise RuntimeError(
                "A2V strict Triton Docker ABI failed; no billed GPU starts: "
                + (strict.stderr or strict.stdout)[-1800:]
            )
        print(strict.stdout.strip())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--require-triton", action="store_true")
    a = p.parse_args()
    verify(a.image, require_triton=a.require_triton)


if __name__ == "__main__":
    main()
