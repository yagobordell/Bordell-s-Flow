# Phase 9 — Formal closure

Status: **CLOSED**

Phase 9 converts the canonical Phase 8 silent clips and the Phase 5 narration timeline into the
finished audiovisual master. The phase is closed after real local validation of the full eight-shot
pipeline, visual inspection of the Remotion renders, final FFmpeg mux validation and green CI.

## Scope completed

```text
9.1  Media probe + frame-exact timeline       CLOSED
9.2  Deterministic captions                   CLOSED
9.3  Remotion visual renderer                 CLOSED
9.4  Transitions + motion overlays            CLOSED
9.5  Final narration mux                      CLOSED
```

The compositor remains isolated from Phase 8 infrastructure. Salad job IDs, LTX profiles, GPU
settings, R2 transport and Postgres state do not cross the Phase 9 domain boundary.

## Canonical flow

```text
VideoClip[] + ShotTiming[] + NarrationWord[]
                    |
                    v
          composition_plan.json
                    |
                    v
       Remotion frame-exact render
                    |
                    v
            visual_motion.mp4
                    |
NarrationAudio -----+
                    |
                    v
             FFmpeg final mux
                    |
                    v
              FinalVideo
```

Python owns semantic validation, frame quantization, caption grouping and presentation props.
Remotion owns deterministic visual rendering only. FFmpeg/ffprobe own media probing and the final
container mux.

## Validated timeline

The canonical composition is:

```text
resolution: 768x1280
fps:        24
frames:     1080
duration:   45.000 s
shots:      8
captions:   29
words:      105
```

Canonical shot frame intervals:

```text
shot 1:    0 ->   84
shot 2:   84 ->  261
shot 3:  261 ->  383
shot 4:  383 ->  450
shot 5:  450 ->  683
shot 6:  683 ->  841
shot 7:  841 -> 1018
shot 8: 1018 -> 1080
```

All boundaries derive from absolute `ShotTiming` values. Source LTX clips may contain extra frames
because of model-specific frame-count constraints, but those extra frames never extend the canonical
timeline.

## Caption validation

Phase 9.2 produced 29 deterministic cues covering all 105 `NarrationWord` IDs exactly once and in
order. Caption words and cues are non-overlapping after frame quantization, and unchanged inputs
reproduce the same composition plan byte-for-byte.

The validated deterministic replay SHA-256 for `composition_plan.json` is:

```text
8E615B10C6C0A9ACAB2B6C1681962D76D1A7946DC67733D94F363C3DD600424F
```

Two upstream transcription display artifacts are corrected only in renderer presentation props:

```text
†el     -> el
sirve†  -> sirve
```

The canonical Phase 9.2 transcription text, IDs and timings remain unchanged.

## Visual renderer validation

Phase 9.3 established the hard-cut baseline `Phase9Visual`. Phase 9.4 added `Phase9Motion` as a
separate visual layer so the accepted baseline remained available for comparison.

The canonical Phase 9.4 profile uses boundary-preserving effects only:

```text
transition_frames:        6
caption_motion_frames:    4
boundary_accent_frames:   5
transition_scale:         1.015
progress_bar:             enabled
```

Effects occur inside existing shot intervals. They do not overlap scenes, shorten the composition or
move canonical shot boundaries.

Real inspection of `visual_motion.mp4` confirmed all seven internal boundaries, no black gaps or
cross-shot overlap, readable captions, stable safe areas and consistent active-word highlighting.

## Final mux validation

Phase 9.5 combines the accepted Phase 9.4 visual master with the canonical Phase 5 narration.

Mux policy:

```text
video: H.264 -> stream copy
audio: WAV   -> AAC 192 kbps
```

The final validated media contract is:

```text
video codec:    H.264 High
resolution:     768x1280
fps:            24
frames:         1080
duration:       45.000 s
audio streams:  1
audio codec:    AAC LC
sample rate:    24000 Hz
channels:       mono
```

The H.264 elementary stream before and after muxing has the same SHA-256:

```text
8e2a95cfb4f3d8c258c3301c550fbcbb7bd626c7da4f2cedc3bcce6d388998ca
```

This proves the final mux preserved the accepted Phase 9.4 video bitstream without re-encoding.
AAC priming is compensated by the MP4 timeline so effective narration starts at 0.000000 seconds.
Audiovisual inspection and waveform-to-word timing checks found no mux-induced narration/highlight
offset or end truncation.

Phase 9.5 also handles streaming WAV headers whose `data` chunk uses `0xFFFFFFFF` as an unknown-size
sentinel. Duration is measured from PCM frames actually present in the file, matching the rule already
used by Phase 5.

## Canonical artifacts

```text
data/output/phase9/
├── composition_plan.json
├── remotion_props.json
├── visual.mp4
├── remotion_props_9_4.json
├── visual_motion.mp4
├── final_video.mp4
└── final_video.json
```

The downstream domain handoff is intentionally small:

```text
FinalVideo = {
  uri,
  duration_seconds
}
```

Renderer settings, FFmpeg flags, AAC bitrate and upstream provider state are implementation details,
not part of final audiovisual identity.

## Reproduction

With Phase 5 and Phase 8 canonical artifacts available:

```powershell
python scripts/run_phase9_compositor.py
python scripts/run_phase9_remotion.py
python scripts/run_phase9_motion.py
python scripts/run_phase9_final.py
```

The final artifact is:

```text
data/output/phase9/final_video.mp4
```

and its provider-neutral metadata is:

```text
data/output/phase9/final_video.json
```

## Verification status

The closure implementation passed repository CI with:

```text
Ruff                 PASS
Pytest               PASS
PowerShell syntax    PASS
Remotion TypeScript  PASS
```

Validated closure workflow before this documentation record:

```text
GitHub Actions run: 34492573346
```

## Documentation

Detailed subphase records:

- [`phase9-compositor.md`](phase9-compositor.md)
- [`phase9.3-remotion.md`](phase9.3-remotion.md)
- [`phase9.4-motion.md`](phase9.4-motion.md)
- [`phase9.5-final-mux.md`](phase9.5-final-mux.md)

## Closure decision

All planned Phase 9 compositor responsibilities are implemented and validated. **Phase 9 is formally
closed.**

Phase 10 must consume `FinalVideo` as its audiovisual input and must not reach back into Remotion,
FFmpeg, Phase 8 GPU transport or LTX implementation details unless a future verification decision
explicitly requests regeneration through an earlier canonical stage.
