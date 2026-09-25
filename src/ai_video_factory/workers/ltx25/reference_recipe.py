from __future__ import annotations

import math
from dataclasses import dataclass

LTX25_MODEL_REVISION = "6c7e5e573ac1667efc83407806fe9b0b93730e60"


@dataclass(frozen=True, slots=True)
class LTXA2VReferenceRecipe:
    """Pinned generation semantics for the first functional avatar lip-sync reference."""

    stage_1_image_strength: float = 0.7
    stage_2_image_strength: float = 1.0
    stage_1_sampler: str = "euler_ancestral"
    stage_2_sampler: str = "euler"
    ancestral_eta: float = 1.0
    ancestral_s_noise: float = 1.0
    ancestral_noise_seed_offset: int = 10_000
    audio_frozen_stage_1: bool = True
    audio_frozen_stage_2: bool = True


LTX_A2V_REFERENCE_RECIPE = LTXA2VReferenceRecipe()


def reference_num_frames_for_samples(
    sample_count: int,
    *,
    sample_rate: int,
    fps: int,
    max_raw_frames: int = 1024,
) -> int:
    """Return the first valid 8k+1 frame count that covers every audio sample.

    Upstream A2V intentionally snaps down so video never outlives its input audio.
    Bordell's avatar contract is the inverse: speech must never be truncated, so
    conditioning audio is padded with silence to the first temporal-grid point
    that covers the decoded waveform.
    """

    if sample_count <= 0:
        raise ValueError("sample_count must be positive")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if fps <= 0:
        raise ValueError("fps must be positive")
    if max_raw_frames <= 0:
        raise ValueError("max_raw_frames must be positive")

    required_frames = max(1, math.ceil(sample_count * fps / sample_rate))
    temporal_steps = max(0, math.ceil((required_frames - 1) / 8))
    num_frames = 1 + temporal_steps * 8
    if num_frames > max_raw_frames:
        raise ValueError(
            "audio duration requires more than the supported LTX temporal grid: "
            f"{num_frames} > {max_raw_frames}"
        )
    return num_frames
