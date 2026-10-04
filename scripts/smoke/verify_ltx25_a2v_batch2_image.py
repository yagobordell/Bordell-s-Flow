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
from ltx_core.block_streaming.builder import StreamingModelBuilder
assert callable(_build_state) and callable(generate_paired_static_a2v)
assert callable(euler_ancestral_denoising_loop)
assert "new_noise_fn" in inspect.signature(euler_ancestral_denoising_loop).parameters
assert "_i2v_progressive_pinned_blocks_supported" in inspect.getsource(StreamingModelBuilder)
assert "generate_pair" in dir(DirectLTX25AudioToVideoBackend)
assert "actual_gpu_batch_size" in inspect.getsource(DirectLTX25AudioToVideoBackend.generate_pair)
assert "stage2_factory" in inspect.signature(generate_paired_static_a2v).parameters
assert hasattr(SharedA2VDecoder, "close")
assert A2VBatch2Parameters.model_fields["stage2_mode"].default == "cpu"
assert ExperimentalA2VBatch2TaskRunner.task_name.endswith("batch2_experimental")
print("LTX25_A2V_B2_DOCKER_ABI_OK torch=" + torch.__version__, flush=True)
"""


def verify(image: str) -> None:
    if not re.fullmatch(r"[^\\s@]+@sha256:[a-fA-F0-9]{64}", image):
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


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    a = p.parse_args()
    verify(a.image)


if __name__ == "__main__":
    main()
