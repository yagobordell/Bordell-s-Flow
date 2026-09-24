# Phase 8 video generation

Phase 8 converts canonical shots into silent, resumable production clips and then upscales them for
composition.

## Motion prompts

`VideoPrompt` is intentionally small:

```text
VideoPrompt = { shot_id, prompt }
```

The prompt describes motion for one continuous shot. Model parameters such as seed, dimensions, fps
and frame count remain workflow/runtime settings rather than semantic domain fields.

Generate prompts directly with:

```bash
python scripts/pipeline/run_phase8_video_prompts.py
```

## LTX fanout and resume

The generation workflow aligns keyframes, prompts and timings by `shot_id`, validates a contiguous
positive timeline and creates deterministic `video.ltx25.generate` requests.

All unresolved shots are submitted before polling so Salad autoscaling can see batch demand.
Operational state is persisted in `video_generation_manifest.json`.

Resume behavior:

| State | Action |
| --- | --- |
| `unsubmitted` | submit |
| `pending` / `running` | resume existing transport |
| `succeeded` | reuse validated response |
| `failed` / `cancelled` | resubmit on an explicit rerun when retry is enabled |

The logical application job ID does not change when a transport is replaced.

Run directly with:

```bash
python scripts/pipeline/run_phase8_videos.py
```

Normal production uses `scripts/pipeline/run_phase8_videos_controlled.ps1`.

## Artifact verification

Keyframes are content-addressed in R2. Completed MP4s are downloaded only after worker response,
object size and SHA-256 metadata validate. Durable R2 replay is checked before unnecessary new GPU
work.

Qwen keyframes are generated natively at 1280x736. Phase 8 accepts these images directly,
without a pre-crop. The LTX worker fits the complete frame proportionally into its 1280x720
output, extending the narrow side margins with edge pixels, then pads only the internal model
grid to 1280x768. Existing native 16:9 keyframes remain accepted.

LTX output is 1280x720 at 24 fps and contains no audio.

## Upscale

`scripts/pipeline/run_phase8_upscale.py` sends accepted clips to the Real-ESRGAN worker. The production
contract requires an exact 2x upscale:

```text
1280x720 -> 2560x1440
24 fps preserved
```

The resulting `upscaled_clips.json` is the Phase 9 video input.

Historical eight-shot closure runs and transport evidence are retained in Git history, not active
documentation.
