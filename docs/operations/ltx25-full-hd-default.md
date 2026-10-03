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
1280×720 and has **no 1080p switch**.

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
