# Phase 9 — Compositor

Phase 9 turns the canonical silent shot clips and narration timeline into a final audiovisual
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
768x1280 dimensions by default, 24 fps by default, no audio streams and enough source frames to cover
the frame-quantized canonical shot interval. Clips that are too short fail rather than being slowed
down or silently padded.

## Phase 9.2 — Deterministic caption track

Status: implemented and validated structurally on the canonical 105-word narration; refined grouping
is pending one final local rerun before closure.

The first canonical local run produced:

```text
shots=8
frames=1080
duration=45.000s
fps=24
captions=29
words=105
```

That run confirmed complete word coverage but exposed two render-facing issues before Remotion:
avoidable one-word cues created by greedy soft limits and a one-frame cue overlap inherited from
slightly overlapping word timestamps. The planner now addresses both deterministically.

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
not silently correct or normalize transcription punctuation or symbols.

A zero-duration narration timestamp is legal upstream. For rendering only, Phase 9.2 expands such a
word to one visible frame. If rounded word timestamps overlap, the later word is advanced only as far
as necessary to produce ordered, non-overlapping display intervals. These are renderer timings and do
not rewrite the canonical Phase 5 timestamps.

### Cue grouping

The default caption profile remains intentionally conservative for vertical short-form video:

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
non-overlapping. Keeping individual frame-quantized words inside each cue allows the future Remotion
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
clips:    data/output/phase8/video_clips.json
timings:  data/output/phase5/shot_timings.json
words:    data/output/phase5/narration_words.json
output:   data/output/phase9/composition_plan.json
profile:  768x1280 @ 24 fps
```

Caption grouping can be tuned for experiments with:

```text
--caption-max-words
--caption-max-chars
--caption-max-duration-seconds
--caption-pause-threshold-seconds
```

The default profile should remain the canonical profile until visual validation in the Remotion
stage gives a concrete reason to change it.

## Phase 9.2 closure criteria

Phase 9.2 can be closed after the refined canonical local run confirms all of the following:

1. all 105 canonical `NarrationWord` IDs appear in captions exactly once and in order;
2. every caption word has a positive visible frame interval inside the 1080-frame composition;
3. neither caption words nor adjacent cues overlap after frame quantization;
4. no avoidable singleton cue is created solely by a soft word/character/duration limit;
5. caption planning leaves the eight shot intervals and `total_frames` unchanged;
6. cue grouping uses only deterministic timing/text rules and makes no provider call;
7. a second invocation with identical inputs produces identical caption JSON;
8. `python -m ruff check .` passes;
9. `python -m pytest` passes.

Remotion rendering, visual caption styling, transitions and final narration muxing remain outside
Phase 9.2.
