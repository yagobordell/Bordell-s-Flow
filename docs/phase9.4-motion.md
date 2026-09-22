# Phase 9.4 — Boundary-preserving transitions and motion overlays

Status: closed after canonical local render, media validation and visual inspection.

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
  - optional thin global progress bar (off by default)

CaptionTrack
  - optional cue and word presentation (off by default)
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
show_captions:              false
show_progress_bar:          false
```

At 24 fps, the default transition window is 250 ms. Python validates that two transition windows can
fit inside every shot and that the boundary accent window fits inside every shot. The visual profile
cannot modify canonical shot boundaries or `total_frames`.

The profile is intentionally not a canonical audiovisual domain contract. It is renderer styling and
may be tuned later without rewriting `ShotTiming`, caption timing or Phase 8 media.

## Optional caption invariants

When captions are enabled, Phase 9.4 reuses the caption cues and word intervals already validated in
Phase 9.2/9.3.
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

The media contract remains exactly:

```text
codec:        H.264
resolution:   768x1280
fps:          24
total frames: 1080
duration:     45.000 s
audio:        none
```

Narration muxing remains outside Phase 9.4.

## Canonical local validation

The real `visual_motion.mp4` was rendered and then independently probed during final review:

```text
codec:          h264
resolution:     768x1280
fps:            24
frames:         1080
duration:       45.000 s
audio streams:  0
file size:      41,311,989 bytes
```

Frame-by-frame boundary inspection covered all seven canonical internal cuts at frames 84, 261, 383,
450, 683, 841 and 1018, including the six-frame transition windows on both sides. The outgoing shot
remains present through its final canonical frame and the incoming shot begins on the exact next
canonical boundary; no black gap, cross-shot blend or accidental overlap was observed.

Comparison against the closed Phase 9.3 `visual.mp4` confirmed that the motion layer changes only
presentation. The opacity dip reaches the configured floor near the boundary and recovers inside the
new shot, while the scale change remains visually subtle. The short boundary accent does not obscure
subjects. Production defaults disable both the progress bar and caption track, so no top duration
indicator or subtitle card is rendered unless explicitly enabled.

When caption rendering is explicitly enabled, caption cards remain readable during their four-frame
motion window, active-word highlighting remains clear, and presentation cleanup still shows `el` and
`sirve` without U+2020 dagger artifacts.
Because Phase 9.4 remains intentionally silent, perceptual voice synchronization is deferred to the
later narration-mux validation; caption and word frame intervals themselves are unchanged.

## Command

No new npm package is required beyond the already installed Phase 9.3 renderer dependencies:

```powershell
python scripts/run_phase9_motion.py
```

The motion profile can be inspected without rendering:

```powershell
python scripts/run_phase9_motion.py --prepare-only
```

Experimental renderer-only tuning remains available through:

```text
--transition-frames
--transition-floor-opacity
--transition-scale
--caption-motion-frames
--boundary-accent-frames
--no-progress-bar
```

The documented defaults are the accepted Phase 9.4 baseline.

## CI validation

The implementation passed the repository CI after the full Phase 9.4 code and documentation landed:

```text
workflow run:          34488188456
Python install:        success
Ruff:                  success
Pytest:                success
PowerShell syntax:     success
Node 22 install:       success
Remotion npm install:  success
TypeScript:            success
```

## Closure criteria

All Phase 9.4 closure criteria are confirmed:

1. `remotion_props_9_4.json` preserves the eight canonical shot intervals;
2. all 29 captions and 105 words remain present with unchanged frame intervals;
3. the motion profile matches the documented default profile;
4. `visual_motion.mp4` is H.264, 768x1280, 24 fps, 1080 frames and contains no audio;
5. visual inspection confirms no black gaps or accidental shot overlap at the seven boundaries;
6. the entry/exit dip and scale remain subtle enough to preserve generated video content;
7. the boundary accent and progress bar remain inside safe areas and do not interfere with captions;
8. caption cue motion remains readable and active-word highlighting remains clear;
9. Python Ruff/pytest and Remotion TypeScript validation pass in CI.

Phase 9.4 is therefore closed. Final narration muxing remains a later Phase 9 subphase.
