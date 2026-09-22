# Phase 9 — Compositor

Status: **CLOSED**

Formal closure record: [`phase9-closure.md`](phase9-closure.md).

Phase 9 turns the canonical Real-ESRGAN-upscaled silent shot clips and narration timeline into a final audiovisual
composition. The compositor is deliberately isolated from Phase 8 GPU transport and LTX internals.

## Phase 9.1 — Media probe and frame-exact timeline

Status: closed and validated against the canonical eight-shot Phase 8 artifacts.

The validated local run produced:

```text
shots=8
frames=1080
duration=45.000s
fps=24
```

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

Durations are then derived from adjacent absolute boundaries. The implementation does not round every
shot duration separately, preventing cumulative drift and preserving contiguous boundaries.

### Source media validation

Each `VideoClip.uri` is resolved below the directory containing `video_clips.json` unless an explicit
base directory is supplied. Escaping that base directory is rejected.

Before a clip enters the composition plan, `ffprobe` must confirm exactly one H.264 video stream,
2560x1440 dimensions by default, 24 fps by default, no audio streams and enough source frames to cover
the frame-quantized canonical shot interval. Clips that are too short fail rather than being slowed
down or silently padded.

## Phase 9.2 — Deterministic caption track

Status: closed and validated against the canonical 105-word narration.

The final canonical local run produced:

```text
shots=8
frames=1080
duration=45.000s
fps=24
captions=29
words=105
```

An unchanged replay produced the same SHA-256 for `composition_plan.json` before and after execution:

```text
8E615B10C6C0A9ACAB2B6C1681962D76D1A7946DC67733D94F363C3DD600424F
```

This confirms deterministic byte-for-byte regeneration for the validated canonical inputs and
current compositor implementation.

The refined output confirms:

- all `NarrationWord` IDs 1 through 105 appear exactly once and in order;
- all caption word intervals are positive and remain inside the 1080-frame composition;
- no caption words overlap after frame quantization;
- no adjacent caption cues overlap;
- the eight canonical shot intervals remain unchanged;
- avoidable singleton cues from the first implementation have been rebalanced;
- only `Lealtad` remains a one-word cue because it is isolated by real narration pauses.

Examples of improved grouping include `poder y territorio`, `dominaban la disciplina`,
`hasta la muerte` and `de los siglos`. The former one-frame overlap between the cues beginning with
`los samuráis dejaron de ser` and `solo soldados y se convirtieron` is also removed: the first cue
ends at frame 922 and the second starts at frame 922.

Phase 9.2 extends the same composition plan with captions derived only from canonical Phase 5 word
timing evidence:

```text
NarrationWord[]
      |
      v
validate ordered words
      |
      v
absolute seconds -> frames
      |
      v
non-overlapping display intervals
      |
      v
hard speech segments
      |
      v
balanced deterministic partition
      |
      v
CaptionCue[]
      |
      v
composition_plan.json captions[]
```

No LLM call is made and caption planning does not reconstruct timing from text. Every rendered word
keeps its canonical `word_id`. Text is preserved verbatim from `NarrationWord`; caption planning does
not silently correct or normalize transcription punctuation or symbols. This means upstream text such
as `†el` and `sirve†` remains visible in the plan until it is corrected at its source or an explicit
presentation-normalization policy is introduced.

A zero-duration narration timestamp is legal upstream. For rendering only, Phase 9.2 expands such a
word to one visible frame. If rounded word timestamps overlap, the later word is advanced only as far
as necessary to produce ordered, non-overlapping display intervals. These are renderer timings and do
not rewrite the canonical Phase 5 timestamps.

### Cue grouping

The default caption grouping remains intentionally conservative while the render surface is now landscape 16:9:

```text
max words per cue:       5
max characters per cue: 36
max cue duration:        2.5 s
pause split threshold:   0.35 s
```

Sentence endings and pauses above the threshold create hard segment boundaries. Inside each hard
segment, the planner evaluates feasible partitions under the word, character and duration limits and
chooses deterministically using these priorities:

1. minimize the number of cues;
2. avoid single-word cues when the segment contains multiple words;
3. prefer cue lengths near four words;
4. use stable longer-first ordering as a final tie-break.

This avoids visual orphans such as a five-word cue followed by a single leftover word when a balanced
3+3 or 4+2 partition fits the same semantic speech segment. A genuinely isolated word separated by a
sentence or long pause may remain alone.

Single words are never discarded merely because they exceed a configured display limit. The limits
control grouping boundaries, not canonical narration content.

### Caption plan contract

`composition_plan.json` contains the internal track:

```text
captions[]:
  id
  text
  start_frame
  end_frame
  word_ids[]
  words[]:
    word_id
    text
    start_frame
    end_frame
```

Caption words are positive-duration, ordered and non-overlapping. Caption cues are also ordered and
non-overlapping. Keeping individual frame-quantized words inside each cue allows the Remotion
renderer to highlight the active spoken word without reading Phase 5 artifacts again.

`CaptionCue` and `CaptionWord` are compositor-internal models. They are not promoted to canonical
audiovisual domain contracts because no downstream semantic phase needs them independently of the
composition plan.

### Command

From the repository root, with `ffprobe` available on `PATH`:

```powershell
python scripts/run_phase9_compositor.py
```

Defaults:

```text
clips:    data/output/phase8/upscaled_clips.json
timings:  data/output/phase5/shot_timings.json
words:    data/output/phase5/narration_words.json
output:   data/output/phase9/composition_plan.json
profile:  2560x1440 @ 24 fps
```

Caption grouping can be tuned for experiments with:

```text
--caption-max-words
--caption-max-chars
--caption-max-duration-seconds
--caption-pause-threshold-seconds
```

The default profile is the closed canonical profile validated by the later Remotion stages.

The production default omits subtitle cues entirely. Use `--include-captions` only for an
intentional captioned render.

## Phase 9.2 closure criteria

All Phase 9.2 closure criteria are confirmed:

1. all 105 canonical `NarrationWord` IDs appear in captions exactly once and in order;
2. every caption word has a positive visible frame interval inside 1080 frames;
3. neither caption words nor adjacent cues overlap after frame quantization;
4. no avoidable singleton cue is created solely by a soft grouping limit;
5. caption planning leaves the eight shot intervals and `total_frames` unchanged;
6. cue grouping uses only deterministic timing/text rules and makes no provider call;
7. a second invocation with identical inputs produces identical caption JSON;
8. `python -m ruff check .` passes in CI;
9. `python -m pytest` passes in CI.

Phase 9.2 is therefore closed.

## Phase 9 final status

Phase 9 is closed end-to-end. The later compositor subphases are documented separately:

```text
9.1  media probe + frame-exact timeline       CLOSED
9.2  deterministic captions                   CLOSED
9.3  Remotion visual renderer                 CLOSED
9.4  transitions + motion overlays            CLOSED
9.5  final narration mux                      CLOSED
```

Detailed records:

- [`phase9.3-remotion.md`](phase9.3-remotion.md)
- [`phase9.4-motion.md`](phase9.4-motion.md)
- [`phase9.5-final-mux.md`](phase9.5-final-mux.md)
- [`phase9-closure.md`](phase9-closure.md)

The canonical final artifact is `data/output/phase9/final_video.mp4` with the provider-neutral
`FinalVideo` metadata artifact at `data/output/phase9/final_video.json`.

The final validated media contract is:

```text
H.264 768x1280 @ 24 fps
1080 frames
45.000 seconds
1 AAC mono narration stream @ 24 kHz
```

Phase 9.5 stream-copies the accepted Phase 9.4 H.264 bitstream, so the final mux cannot change the
closed visual timeline. The elementary video stream SHA-256 is identical before and after muxing:

```text
8e2a95cfb4f3d8c258c3301c550fbcbb7bd626c7da4f2cedc3bcce6d388998ca
```

Audio begins at the canonical zero origin after normal AAC priming compensation and ends at the
45-second composition boundary. Objective waveform-to-word interval validation confirms that muxing
introduces no narration/highlight offset. The final render also preserves the presentation-only
caption cleanup introduced in Phase 9.3 without mutating Phase 9.2 canonical transcription text.

Phase 9 therefore hands downstream delivery or verification stages a finished audiovisual asset
without exposing Remotion, FFmpeg, Salad, LTX or GPU transport details in the `FinalVideo` domain
contract.
