# Phase 9 compositor

Phase 9 turns the Real-ESRGAN-upscaled silent clips and canonical narration timeline into
`FinalVideo`.

## Timeline

`ShotTiming` is authoritative. Source clips may contain extra rounded frames; the compositor does not
stretch the production timeline to consume them.

Absolute second boundaries are quantized independently to frames and adjacent shot intervals remain
contiguous.

Before planning, `ffprobe` validates each source clip. Production defaults are:

```text
2560x1440
24 fps
H.264 video
no source audio
```

Clips must contain enough frames to cover their canonical intervals.

## Captions

Captions are deterministic and optional. `NarrationWord[]` is converted to non-overlapping frame
intervals without changing canonical upstream timestamps. Cue grouping is based only on timing/text
rules; no model call is used.

The production default omits captions. Use `--include-captions` for an intentional captioned render.

## Commands

Build the validated composition plan:

```powershell
python scripts/pipeline/run_phase9_compositor.py
```

Targeted Phase 9 rendering/finalization is available through:

```text
scripts/pipeline/run_phase9_motion.py
scripts/pipeline/run_phase9_final.py
```

Normal end-to-end production invokes Phase 9 through `run_video_factory.ps1`.

## Output contract

The final artifact is:

```text
data/output/phase9/final_video.mp4
```

FFmpeg muxes canonical narration onto the accepted visual stream. The final stage must not regenerate
model video merely to add audio.

Historical closure metrics, frame hashes and old vertical-profile evidence belong in Git history.
