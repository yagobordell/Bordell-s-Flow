# End-to-end performance

This document is the measurement contract for the optimized Video Factory. It separates measured
evidence from architectural expectations so latency improvements are never reported from invented
numbers.

## Optimization target

The objective is total wall-clock from the canonical finished script to `FinalVideo`.

Quality is held constant:

- same domain contracts;
- same production providers and model revisions;
- same generation profiles;
- same image/video dimensions;
- same fps and canonical narration timeline;
- same seeds where a deterministic seed exists;
- same safety/fallback classifications;
- same Phase 9 visual profile;
- final FFmpeg mux keeps `-c:v copy`.

Resolution, frame count, diffusion quality, alignment or validation are not reduced to obtain speed.

## Critical-path model

The pre-optimization production runner executed a phase list serially even where data dependencies
did not require it. GPU lifecycle was also scoped to each controlled phase wrapper, so model startup
could not overlap unrelated CPU/LLM work and a shared model could be stopped between two uses in the
same video.

The optimized DAG exposes these independent branches:

```text
source script
    |
    v
phase2 narrative
    |----------------------------------|
    v                                  v
continuity                         narration/Breeze
    |----------------|                 |
    v                v                 v
shots          reference prompts   Whisper alignment
    |                |                 |
    |                v                 v
    |        reference assets       beat timing
    |          [Ideogram]              |
    |                                  v
    +--------------------------- shot timing
                                       |
                                  storyboard
                                  /         \
                                 v           v
                          video prompts   keyframes
                                        [Ideogram]
                                  \         /
                                   v       v
                                   LTX clips
                                       |
                                       v
                              Remotion + FFmpeg
                                       |
                                       v
                                  FinalVideo
```

Reference assets remain required production artifacts, but current keyframe inference consumes the
structured storyboard prompt rather than the Phase 4 PNG bytes. The scheduler therefore does not add
a false binary dependency between these branches.

## Measured baseline evidence already available

There was no repository-wide end-to-end timer on the original
`99dfe1821b06cd5e3e23a3523b9bd95a89b264ac` path, so this document does **not** invent a total
"before" time.

Existing real cloud evidence nevertheless proves that startup can dominate inference. The validated
Fish Speech fallback smoke recorded approximately:

```text
assignment_seconds                 20.9
image_pull_and_start_seconds    1,884.0
container_started_seconds       1,905.0
bootstrap_after_start_seconds   1,540.1
ready_seconds                   3,445.1
inference_seconds                  25.479
audio_seconds                      16.992
roundtrip_seconds                  35.031
R2 replay with worker stopped       0.502
```

That evidence is not used as an end-to-end speedup claim. It demonstrates why cache-before-GPU,
prewarm overlap and bounded worker retention have much larger potential wall-clock impact than
micro-optimizing Python serialization.

The previously validated LTX benchmark also remains an inference reference, not an end-to-end
baseline: 121 frames at 768x1280 averaged approximately 194.93 seconds on the documented RTX 5090
profile.

## Implemented optimizations

| Change | Expected critical-path effect | Quality effect | Cost control |
| --- | --- | --- | --- |
| Explicit dependency DAG | overlaps independent CPU/LLM/GPU stages | none | total/GPU stage limits |
| Breeze cache audit before prewarm | removes speech cold start on valid replay | none | zero Breeze/Fish GPU on hit |
| Whisper cache audit before prewarm | removes alignment cold start on valid replay | none | zero Whisper GPU on hit |
| Keyframe cache audit before prewarm | removes image cold start on complete replay | none | invalid cache fails early |
| LTX R2 replay before queue | avoids allocation/submission after local-state loss | none | zero LTX GPU on full hit |
| Temporary Ideogram hold Phase 4 -> 6 | avoids stop/restart inside one video | none | bounded to end-to-end mode |
| Model resource key | prevents oversubscription of constrained shared service | none | one same-model stage at once |
| Qwen scale-to-zero lifecycle | avoids idle image GPU cost | none | GPU only for uncached image work |
| Global preflight | avoids discovering config errors after startup | none | no GPU before checks |
| Outer finally cleanup | prevents leaked warm workers after failures | none | stopped + replicas=0 + clean queues |

Qwen-Image-2.1 is the only active image generator. Its worker remains scale-to-zero and is prewarmed only when uncached image work exists. The guard waits for `status=stopped`, `replicas=0` and
`pending_change=false`. The outer runner then invokes queue cleanup for every configured service.

A benchmark is not accepted if these cleanup conditions are not demonstrated.

## Decision log: concurrency

No worker-level inference concurrency or Salad `max_replicas` value is increased by this PR without
real GPU evidence.

In particular:

- Fish remains `max_replicas=1` and one inference per replica;
- current Ideogram service limits are respected;
- LTX keeps its existing manifest limits;
- the new scheduler only overlaps **different** resource services within the configured global GPU
  bound.

Future concurrency increases require a measured VRAM/throughput benchmark and are separate from this
safe orchestration optimization.
