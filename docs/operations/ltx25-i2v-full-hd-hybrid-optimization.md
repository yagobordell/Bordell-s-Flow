# Full-HD true B=2 I2V: hybrid GPU/CPU FP8 experiment

**Status:** Source-level experiment only; a fresh RTX 5090 paid trial, side-by-side visual review and host-memory monitoring are required. **DO NOT merge the historical mixed I2V/A2V PR.** A2V worker/profile, its concurrency and ongoing external optimizations remain untouched.

## Why the previous 1080p run was slow

The first native 1920×1080, true-GPU-B=2 trial with a resident FP8 transformer OOMed during stage2. Its two-prompt Gemma and true-B2 first stage succeeded; the latter needed ~29.7s denoising and ~7.7s model construction.

For the subsequent *successful* DISK-streaming full-HD pair, the attached 2026-10-04 Salad log measured: ~286s and ~297s to encode the two prompts, ~1,634s first-stage diffusion, ~924s second-stage diffusion, and 3,407.85s total (~56m48s). The successful job used 17.0 GiB CUDA peak allocated and emitted both 1920×1080 / 24fps / 121-frame MP4s without falling back to serial inference. However, the process RSS rose to ~37.4 GiB during stage2 and the disk-read counters grew from ~6 to >116 GB during the run. These results do **not** prove the host would handle all-pinned CPU weight buffers with only 40 GiB RAM.

## New implementation — I2V experimental mode only

1. **Stage1 is GPU-resident FP8 (NONE)**: restore the fast true-B=2 method already shown to complete the first full-HD stage. Two prompts are encoded separately as in the approved 720p baseline, before the transformer is constructed. Do not keep the stage1 model live across the upsampler or stage2.
2. **After disposing stage1, upscale both independent latents unchanged.** Release individual Gemma outputs, no-longer-needed stage1 conditioning and stage1 video tensors at safe boundaries. Preserve original stage1 audio latent, separate seeds and identical two-stage denoising sigmas.
3. **Build only an alternate stage2 wrapper lazily AFTER full-size image encoding.** Default `-FullHdStage2Mode cpu` uses LTX upstream's same FP8_CAST model and `OffloadMode.CPU`: blocks are pinned in host RAM **once** and transferred to the GPU per block across three full-resolution true-B=2 denoising forwards. This avoids the successful-but-slow DISK path, which re-reads blocks from the local checkpoint on every step. Only the stage wrapper is built, not a second Gemma/VAE pipeline.
4. **Host-memory prerequisite:** the isolated Salad *I2V-only* group requests **65536 MiB**, up from 40960. The PowerShell trial fails local preflight before starting Salad if the tracked I2V manifest still requests less than 64 GiB for CPU mode. This is an experimental minimum, **not a proof** the largest CPU pinned-weight load will fit. Salad machine availability and price may change at 64 GiB; ensure the prepared remote I2V group actually has the new resource setting and that A2V is idle. Never resize or interrupt A2V.
5. **Explicit fallback for investigation:** `-FullHdStage2Mode disk` keeps the bounded two-slot CPU DISK streamer for stage2 only. It does *not* silently downgrade an attempted CPU job. 720p B2 remains unchanged and fully GPU resident. Full-HD always retains genuine GPU forward batch=2 and no sequential fallback.
6. Log the selected stage1/stage2 offload policy, stage2 CPU buffer policy, per-stage elapsed/build time, per-forward VRAM and host counters. The report labels the hybrid mode as `stage1:none,stage2:cpu` or `stage1:none,stage2:disk`.

The pipeline does not change the transformer checkpoint, FP8_CAST recipe, final 1920×1080 geometry (internal 1920×1088 padded/cropped grid), 121 frames, 24fps, sampler schedules, independent random seeds, conditioning arithmetic or VAE. CPU versus DISK loading should not change numerical model weights; generated videos still require direct quality verification.

## Free validation and next paid decision

- Use the branch-scoped I2V CI first: Ruff, paired worker contracts, lazy-stage build ordering, exact FP8 policy, no hidden B1, Full-HD geometry and PowerShell safe preflight.
- A **new Docker digest** is required: this is a worker-source change, unlike the earlier Salad 522 polling fix. Re-Prepare ONLY the stopped I2V Salad service with the new digest and 64-GiB host RAM; verify remote resources before paying.
- New unique RunId and two preferably distinct 1920×1080 source PNGs; use `-Width 1920 -Height 1080 -AcceptFullHdBatch2OomRisk -DisableStageReuse -FullHdStage2Mode cpu -CheckInputsOnly` first. Compare same-seed/prompt outputs, technical validation, max GPU and host memory, CPU major page faults, per-stage latency and startup cost against the 56m48s DISK baseline.
- CPU pinned weight startup may still exhaust host RAM or be affected by poor storage; **do not assert the time or memory problem is solved** until real hardware evidence supports it. Do not auto-retry expensive CPU failures in DISK mode and do not enable this experiment in the normal B2 dispatcher.

**Quality gate:** visual review against matched 1080p B=1 reference frames and lipsync checks remain separate; the observed latest paired test used the SAME initial PNG hash for both outputs, so acquire two distinct sources for broader visual acceptance.
