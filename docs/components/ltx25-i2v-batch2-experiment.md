# LTX 2.5 I2V: instrumented optimization experiment

This experiment is **not a production rollout**. It changes neither the default
`generation_profile` nor the tracked Salad image/digest. Run controlled paid
RTX 5090 tests before approving the experimental profile.

## Independent recipes

| CLI `--i2v-profile` | CFG Stage 1 batch | Final audio decode | DiffVAE |
|---|---:|---|---|
| `default` | 1 | enabled (historical) | eager |
| `cfg-b2` | 2 | enabled | eager |
| `cfg-b2-silent` | 2 | skipped | eager |
| `cfg-b2-triton` | 2 | skipped | Triton if available, eager if not |
| `static-camera` | 1 | enabled | eager |

All recipes retain distilled 8+3 steps, positive and negative prompts,
effective negative CFG 1.25, joint audio/video diffusion, original seed,
conditioning strength 1.0, 1280x768 internal generation, 1280x720 delivery,
24fps and silent H.264 MP4 output.

`cfg-b2` isolates the effect of CFG batching; `cfg-b2-silent` isolates
the removal of the final discarded audio decoder; `cfg-b2-triton` isolates
the DiffVAE backend. Each has a **different application job identity / R2
key** so it cannot replay another recipe's cached result.

## Observability

`LTX25_I2V_PHASE_METRICS` logs one JSON record per completed generation:
pipeline acquisition, prompt encoding, two image conditioning phases,
two stage durations, spatial upsampling, lazy video decode (during encode),
optional final audio decode, encode including decode, estimated encode
excluding decoder, full time, observed peak CUDA allocation and reservation,
requested/effective decoder backend, stage-1 batch, the **expected** transformer
forward counts (not direct instrumentation), pipeline reuse and geometry.

CUDA synchronization is enabled for comparable phase timings. This changes
timing overhead but not model inputs or random state. Encode-exclusive is a
derived approximation, not an independently profiled FFmpeg/kernel measurement.
Timing of internal model-context setup is included in each stage total, not
separately attributed.

## Safety and rollout

- NATTEN is **always disabled** due to the previously observed 5090 segmentation
  fault. The experimental request checks Triton availability *before* building
  the pipeline. If unavailable, the effective backend is logged as eager.
- A Triton kernel failure during GPU work is **not** caught and retried in the
  same CUDA process: the existing GPU-failure handling invalidates the affected
  pipeline for normal retry. An eager-only profile is the rollback.
- Only one I2V pipeline is retained at a time. Changing requested decoder
  backend rebuilds it from locally cached checkpoints; benchmark that startup
  separately from steady-state generation.
- Stage 1 batch 2 can increase peak activation VRAM and cause OOM. Keep batch 1
  as the stable baseline and never auto-promote batch 2 on an untested 5090.
- The transformer still jointly generates video and audio latents. The silent
  optimization skips only the final waveform decoder that was previously
  discarded. The MP4 format remains silent.

## Controlled validation

Use a reviewed image pinned by digest **built from this branch**, keeping the
A2V Salad group stopped. The existing smoke script supports `-I2VProfile`
`default`, `cfg-b2`, `cfg-b2-silent` and `cfg-b2-triton`. Use
`-CompareWithRunId` to reuse baseline input image, prompts, timing and seed.

Run baseline and each experimental profile on the same real RTX 5090, first as
short 2-second smokes, then repeated 5-second comparisons. Check per-phase
latency and p50/p90 total, 0 OOM/segfault, observed decoder backend and VRAM.
Review generated clips side-by-side for subject/face/hands, background,
camera zoom and flicker. Validate ffprobe 1280x720/24fps/silent H.264, the
complete keyframe frame and subsequent 2560x1440 upscale. Running offline
pytest and CI does **not** validate visual quality or live Triton kernels.

Do not merge, change the production `generation_profile` or update Salad
manifests until controlled GPU/visual sign-off.
