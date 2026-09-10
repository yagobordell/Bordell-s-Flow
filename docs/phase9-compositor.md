# Phase 9 — Compositor

Phase 9 turns the canonical silent shot clips and narration timeline into a final audiovisual
composition. The compositor is deliberately isolated from Phase 8 GPU transport and LTX internals.

## Phase 9.1 — Media probe and frame-exact timeline

Status: implemented, pending local validation against the canonical eight-shot Phase 8 artifacts.

The Phase 9.1 input boundary is:

```text
VideoClip[] + ShotTiming[]
          |
          v
     ffprobe clips
          |
          v
 absolute timing boundaries
          |
          v
   frame quantization
          |
          v
 composition_plan.json
```

`ShotTiming` remains authoritative. Generated LTX clips may contain extra rounded frames; Phase 9.1
never stretches the canonical timeline to match those source durations.

### Frame quantization

Shot boundaries are converted independently from absolute seconds to frames using round-half-up:

```text
start_frame = round_half_up(start_seconds * fps)
end_frame   = round_half_up(end_seconds * fps)
```

Durations are then derived from adjacent absolute boundaries:

```text
duration_frames = end_frame - start_frame
```

The implementation does not round every shot duration separately. This prevents cumulative drift
across a long sequence and guarantees that adjacent canonical shot boundaries remain contiguous.

### Source media validation

Each `VideoClip.uri` is resolved below the directory containing `video_clips.json` unless an explicit
base directory is supplied. Escaping that base directory is rejected.

Before a clip enters the composition plan, `ffprobe` must confirm:

- exactly one video stream;
- H.264 codec;
- 768x1280 dimensions by default;
- 24 fps by default;
- no audio streams;
- enough source frames to cover the frame-quantized canonical shot interval.

If `nb_frames` is unavailable, Phase 9.1 derives an available-frame estimate from the measured
container duration and expected fps. Clips that are too short fail rather than being slowed down or
silently padded.

### Composition plan

The generated `data/output/phase9/composition_plan.json` is an internal rendering contract, not a
new canonical audiovisual domain model. It contains:

```text
schema_version
width
height
fps
total_frames
shots[]:
  shot_id
  uri
  start_frame
  end_frame
  duration_frames
  source_duration_seconds
  source_frame_count
```

The resolved source URI is local because the later Remotion renderer will run on the same compositor
host. A future Phase 9 manifest will identify reusable renders by content hashes rather than by this
machine-specific path.

### Command

From the repository root, with `ffprobe` available on `PATH`:

```powershell
python scripts/run_phase9_compositor.py
```

Defaults:

```text
clips:    data/output/phase8/video_clips.json
timings:  data/output/phase5/shot_timings.json
output:   data/output/phase9/composition_plan.json
profile:  768x1280 @ 24 fps
```

Alternative inputs can be supplied with `--clips`, `--timings`, `--clip-base-dir`, `--width`,
`--height`, `--fps` and `--output`.

## Phase 9.1 closure criteria

Phase 9.1 can be closed after the canonical eight-shot Phase 8 output is run locally and all of the
following are confirmed:

1. all eight `VideoClip` IDs match the ordered `ShotTiming` IDs;
2. all eight MP4 files pass the source-media checks;
3. the first composition interval starts at frame 0;
4. every adjacent interval is contiguous;
5. no frame interval has zero or negative duration;
6. every source clip has enough media for its canonical interval;
7. the final `total_frames` equals the quantized final `ShotTiming.end_seconds` boundary;
8. `python -m ruff check .` passes;
9. `python -m pytest` passes.

Remotion rendering, captions, transitions and final narration muxing remain outside Phase 9.1.
