# CURRENT I2V HYBRID UPDATE (supersedes old all-DISK experiment)

The measured October 4 all-DISK 1080p true-B2 run succeeded (3,407.85s)
with ~17.0 GiB peak GPU allocated but unacceptable repeated disk reads.
The NEXT, **unverified** I2V-only trial now uses a GPU-resident FP8 stage1
and full-block pinned-RAM FP8 stage2, currently requesting **61440 MB host RAM** from Salad. This reduction from the original 65536-MB budget is unvalidated; RAM pressure must be verified.
for the I2V Salad service **only**. Existing 720p remains unchanged.
The older all-DISK performance notes further down record historical
experiments, not the current selected mode.

See [hybrid 1080p I2V implementation](ltx25-i2v-full-hd-hybrid-optimization.md)
for the dedicated resource prerequisite, exact stage ordering, independent
FP8 and B2 quality contracts, safe no-cost preflight and why GPU success
and speed improvement are NOT YET proven. A2V worker optimizations are
out of scope; never merge this historical conflicted PR wholesale.

---

# New video default: 1920×1080 / 24 fps — EXPERIMENTAL, NOT MERGED

New **B2 video jobs** (professional and casero), default standalone Phase 8
I2V jobs, new A2V request defaults, final B2 composition, and casero CTA
A2V+composition now target 1920×1080 at 24 fps. **This changes only newly
constructed jobs** and their immutable fingerprints; previously submitted
1280×720 job requests retain their original explicit parameters, output
keys and video bytes.

**GPU acceptance has NOT been established for 1080p I2V in the existing
32-GiB RTX 5090.** Do not merge, deploy as production defaults, or assume
two 1080p videos fit on one GPU until a separate I2V and long-audio A2V trial
has confirmed memory, FPS, timing and visual quality/lip synchronization.
The successful *old* GPU batch2 experiment validated TWO 720p outputs, not
1080p. The ongoing October 3 optimized batch2 720p Salad trial is unchanged.

## Resolution and quality contract

- Public Full HD 1920×1080. Native LTX two-stage internal geometry
  1920×1088 (height rounds up to the 64-pixel grid). The edge-padded
  reference image is centered and the decoded video is centrally cropped
  back to exactly 1920×1080, **not stretched**. No new model, quantization,
  interpolation of output frames, frame cadence or speech sampler.
- Full HD rendering on top of an existing 1280×720 or Qwen 1280×736 source
  still starts with THAT source's level of visual detail. LTX generates
  full-HD OUTPUT pixels, but this does not magically create an independent
  high-resolution source photograph. 1920×1080 reference images should be
  generated upstream for the ideal full-HD visual input quality.
- B2 still-image beats now render **directly from their original PNG** at
  1920×1080, including high-resolution/x2 image assets. They never first
  discard detail by making a 720p preview and scaling it back up for the
  final Ken Burns clip. The interactive static preview still defaults 720p.
- Existing fast 576p → public 720p LTX profiles and isolated true-B2 pair
  smoke are deliberately unchanged. They remain **explicit legacy /
  experimental recipes**, not the default professional output.
- The separate original Phase 8 720p → Real-ESRGAN 1440p path is a LEGACY
  explicitly run upscale stage. It is not repurposed to rescale fresh 1080p
  jobs and must not be mixed with Full HD clips or a 1440p-only Phase 9
  manifest. The modern B2 output is composed directly at 1080p.

## Regression and cache safety

All newly generated B2 I2V and A2V job IDs use their actual width/height.
The B2 manifest fingerprint now includes resolution when not historical
1280×720. Previously paid v4 A2V output is not imported into a Full HD
video: inheritance is restricted to explicitly chosen legacy 720p B2 jobs.
The compositor validates that all generated clips have the SAME requested
resolution and checks the final mux at that exact size. It keeps Ken Burns
motion, original narration and captions unchanged.

Casero CTA requests use the B2 video size too. A cached CTA produced
without resolution markers is treated as historical 720p and **refused**
for new 1080p delivery: use a fresh CTA asset directory rather than
silently upscaling/reusing an earlier paid result.

For explicitly approved 720p smoke jobs, the old runner scripts pass
`--width 1280 --height 720`; the current 720p batch2 trial accepts only
1280×720 and retains **720p as its default**; the opt-in paired trial now exposes an explicitly authorized Full HD test switch.

## Agreed scope: I2V parallel B=2 only; A2V stays single-video

The intended final default is **1920×1080** in both I2V and A2V.
Only I2V is intended to run TWO independent videos at once on one RTX
5090. A2V must remain independent/single-video; its worker and ongoing
external optimization branch are out of scope for this follow-up.
The existing shared B2 orchestrator issues explicit 1080p requests;
do not change the A2V worker code here while the parallel A2V work is underway.

After the successful 720p native batch2 visual/performance campaign, the
isolated **I2V-only** paired job and trial runner support an explicitly
authorized, native **1920×1080 B=2** five-second experiment. This route
requires `accept_full_hd_batch2_oom_risk=true` in its worker request and
`-AcceptFullHdBatch2OomRisk -Width 1920 -Height 1080` in the PowerShell
trial runner. Full-HD source inputs must each be 1920×1080 PNG, preferably
from the upstream native Full HD image generation rather than a resized
720p starting frame. The 720p experiment remains its default and is
unchanged. No A2V code was modified by this I2V-only extension.

The full-HD pair experiment forbids sequential fallback and shared-stage
GPU-transformer residency. If it exhausts VRAM, it FAILS with a visible OOM
report rather than silently generating at 720p or one at a time. A passed
CPU/unit preflight only confirms the input/worker contract: GPU peak,
speed, output visual identity and hardware fit are **NOT yet known**.
Do not enable automatic B=2 1080p in normal B2 dispatch until its dedicated
RTX 5090 trial succeeds. If it cannot fit on 32-GiB hardware, re-evaluate
GPU capacity and scheduling explicitly; do not reduce output quality.

## Postmortem and VRAM reduction after the failed 1080p test

The first paid native two-video 1920×1080 test **did not succeed**:
it completed BOTH Gemma prompts, the first true-B2 FP8 transformer stage
and upsampling, then OOMed in the stage2 transformer with PyTorch
peak reserved VRAM of roughly 30.5 GiB on the 31.8-GiB device. The
full-size stage2 activation requirement was not measured exactly.

**New I2V-only unproven mitigation**: full-HD B2 now uses LTX upstream's
DISK block-streaming mode with the exact FP8_CAST quantization policy
and the same max forward batch=2, steps, seeds, prompts and geometry.
Streaming has only `DISK_CPU_SLOTS=2` pinned host buffer slots, rather
than pinning the full model in the 40-GiB host. The transformer is still
one shared model per stage and each forward is genuinely B=2, not two
B=1 calls. This substantially reduces persistent GPU model weight
residency at the price of repeated local checkpoint/block I/O and
potentially much higher latency; do NOT promise a specific speed.

Separately the paired I2V pipeline drops the redundant individual
Gemma embedding outputs after concatenation, drops stage1 conditioning
immediately after stage1, drops its video latent after the unchanged
independent upsampler calls, retains only required stage1 AUDIO latent
for stage2, and trims otherwise unreachable CUDA cached blocks at
full-HD stage boundaries. Read-only per-transformer-forward and
per-stage GPU snapshots now distinguish persistent weight pressure
from transient full-HD activations. The visually approved 720p paired
path is still GPU-resident / full B=2 and does not trim GPU memory at
each stage.

This reduction is implemented and CPU-regression-tested only. Only a
fresh pinned-image paid RTX 5090 trial can determine hardware fit,
runtime and visual equivalence. Never silently reduce resolution, FPS,
precision, denoising steps or GPU batch to make an experiment pass.
The user is separately optimizing A2V: **this follow-up does not
change any A2V worker files, profiles or audio/lipsync code**.
The old multi-purpose experimental PR is currently behind main and
has merge conflicts; do NOT merge it wholesale into current A2V work.

## Next validation (after the currently-running 720p trial completes)

1. Verify Full HD model-grid and request/manifest contract unit tests and
   full CI. Do not update the image while the 720p Salad trial is active.
2. In a NEW pinned Docker image, perform **one single-video I2V 1080p**
   smoke with a full-HD source image if available. Record GPU peak VRAM,
   per-stage transformer builds, 1920×1080/24-fps MP4, runtime and motion.
   Use only the unchanged visually approved FP8/distilled profile.
3. Perform a separate 1080p A2V test using the previously approved
   talking-head profile and a representative longest expected audio clip.
   Verify speech timing and lip synchronization visually against the
   original audio, output duration and decoder memory.
4. Only consider 1080p GPU batch2 after both individual modes pass.
   Two 720p videos consumed ~25.4 GiB; two 1080p videos are NOT
   demonstrated to fit in 32 GiB, regardless of whether source code
   accepts 1080p. Do not silently reduce the resolution, frame count or
   model quality to work around OOM.

The Full HD code remains in the existing **unmerged experimental branch**.
The active 720p paid-trial Docker digest is immutable and unaffected by
these later source commits.
