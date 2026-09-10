# Phase 9.4 — Boundary-preserving transitions and motion overlays

Status: implemented, CI-validated, pending canonical local render and visual validation.

Phase 9.4 adds visual polish on top of the closed Phase 9.3 renderer without changing the canonical
Phase 9 timeline. It consumes the same closed `composition_plan.json`; no semantic or timing decision
is reconstructed in TypeScript.

## Timing rule

Remotion's `TransitionSeries.Transition` overlaps adjacent scenes and shortens the series duration.
That behavior is intentionally not used here because `ShotTiming` and the already quantized shot
boundaries remain authoritative.

Instead, every visual transition is contained inside the shot that already owns those frames:

```text
shot N final frames        canonical cut        shot N+1 first frames
       |                         |                         |
       v                         v                         v
subtle exit dip/zoom       unchanged frame       subtle entry dip/zoom
```

The source `Sequence` for every shot keeps exactly the same `start_frame`, `end_frame` and
`duration_frames` as Phase 9.3. There is no cross-shot overlap, added frame, duplicated frame or
transition-driven timeline compression.

## Renderer composition

Phase 9.3 remains available as the baseline `Phase9Visual` composition. Phase 9.4 adds a separate
`Phase9Motion` composition so the validated hard-cut render can still be reproduced independently.

The Phase 9.4 composition contains three visual layers:

```text
MotionShotTrack
  - canonical shot Sequence intervals
  - short entry/exit opacity dip
  - subtle scale animation

MotionOverlayTrack
  - short light sweep after internal cuts
  - thin global progress bar

CaptionTrack
  - unchanged cue and word frame intervals
  - subtle cue entrance/exit translation and scale
  - unchanged active-word highlighting
```

All effects are deterministic functions of the current Remotion frame and renderer props. There are
no random values, provider calls or media-analysis decisions inside the renderer.

## Renderer-only visual profile

`remotion_props_9_4.json` uses schema version `2` and includes a renderer-only `visual_profile`:

```text
transition_frames:          6
transition_floor_opacity:   0.72
transition_scale:           1.015
caption_motion_frames:      4
boundary_accent_frames:     5
show_progress_bar:          true
```

At 24 fps, the default transition window is 250 ms. Python validates that two transition windows can
fit inside every shot and that the boundary accent window fits inside every shot. The visual profile
cannot modify canonical shot boundaries or `total_frames`.

The profile is intentionally not a canonical audiovisual domain contract. It is renderer styling and
may be tuned later without rewriting `ShotTiming`, caption timing or Phase 8 media.

## Caption invariants

Phase 9.4 reuses all 29 caption cues and 105 word intervals already validated in Phase 9.2/9.3.
Caption motion only changes presentation opacity/translation/scale inside each existing cue interval.
It does not move cue starts, cue ends, word starts or word ends.

The Phase 9.3 presentation normalization remains active, so word IDs 41 and 43 render as `el` and
`sirve` while the closed canonical transcript remains untouched.

## Outputs

Phase 9.4 writes separate artifacts so the Phase 9.3 baseline is preserved:

```text
data/output/phase9/remotion_props_9_4.json
data/output/phase9/visual_motion.mp4
```

The expected media contract remains exactly:

```text
codec:        H.264
resolution:   768x1280
fps:          24
total frames: 1080
duration:     45.000 s
audio:        none
```

Narration muxing remains outside Phase 9.4.

## Command

After pulling the implementation, no new npm package is required beyond the already installed
Phase 9.3 renderer dependencies:

```powershell
python scripts/run_phase9_motion.py
```

The motion profile can be inspected without rendering:

```powershell
python scripts/run_phase9_motion.py --prepare-only
```

Experimental renderer-only tuning is available through:

```text
--transition-frames
--transition-floor-opacity
--transition-scale
--caption-motion-frames
--boundary-accent-frames
--no-progress-bar
```

The defaults remain the canonical Phase 9.4 validation profile until visual review gives a concrete
reason to change them.

## CI validation

The implementation passed the repository CI after the full Phase 9.4 code and documentation landed:

```text
workflow run: 34488188456
Python install:      success
Ruff:                success
Pytest:              success
PowerShell syntax:   success
Node 22 install:     success
Remotion npm install: success
TypeScript:          success
```

The CI validates the renderer props schema, motion-profile invariants and TypeScript integration. It
does not render the private local Phase 8 MP4 artifacts, so the real 1080-frame render remains the
final acceptance step.

## Closure criteria

Phase 9.4 closes after the canonical local run confirms:

1. `remotion_props_9_4.json` contains the eight unchanged canonical shot intervals;
2. all 29 captions and 105 words remain present with unchanged frame intervals;
3. the motion profile is exactly the documented default profile;
4. `visual_motion.mp4` is H.264, 768x1280, 24 fps, 1080 frames and contains no audio;
5. visual inspection confirms no black gaps or accidental shot overlap at the seven boundaries;
6. the entry/exit dip and scale remain subtle rather than obscuring generated video content;
7. the boundary accent and progress bar remain inside safe areas and do not interfere with captions;
8. caption cue motion remains readable and active-word highlighting remains clear;
9. Python Ruff/pytest and Remotion TypeScript validation pass in CI — confirmed.

Final narration muxing remains a later Phase 9 subphase.
