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
| Keyframe cache audit before prewarm | removes image cold start on complete replay | none | invalid cache fails early |
| LTX R2 replay before queue | avoids allocation/submission after local-state loss | none | zero LTX GPU on full hit |
| Temporary Ideogram hold Phase 4 -> 6 | avoids stop/restart inside one video | none | bounded to end-to-end mode |
| Model resource key | prevents oversubscription of constrained shared service | none | one same-model stage at once |
| FLUX scale-to-zero arm | avoids eager fallback GPU in common case | none | GPU only after safety need |
| Global preflight | avoids discovering config errors after startup | none | no GPU before checks |
| Outer finally cleanup | prevents leaked warm workers after failures | none | stopped + replicas=0 + clean queues |

The FLUX policy has an explicit trade-off: a newly discovered safety rejection may incur FLUX cold
start on that rare fallback path because FLUX is no longer always prewarmed. Persisted safety evidence
allows deterministic prewarm on later identical runs. This reduces routine cost/GPU contention
without changing fallback output quality.

Fish does not use this speculative policy at all. It remains strictly downstream of a classified
Breeze failure and a verified Breeze zero-replica state.

## Metrics written by the runner

`data/output/production_metrics.json` records phases 2-8:

```text
total_elapsed_seconds
serial_stage_seconds
critical_path_seconds
dag_overlap_saved_seconds
max_parallel_stages
max_parallel_gpu_stages
stages[].stage_name
stages[].outcome
stages[].resource
stages[].elapsed_seconds
```

The values are measured on the same invocation. `serial_stage_seconds` is useful as a
same-run serial-equivalent reference for the scheduler: it is the sum of stage wall clocks, while
`total_elapsed_seconds` includes actual overlap. It is **not** presented as the historical
pre-optimization baseline because controlled-stage lifecycle has also changed.

`data/output/video_factory_metrics.json` adds:

```text
total_wall_clock_seconds
production_phases_2_8_seconds
phase9_seconds
final_video
manual_intervention
cleanup
```

Controlled Salad scripts continue to emit model-specific startup information such as assignment,
image pull, bootstrap, readiness and inference timings when those values are observable.

## Cache/run classes

Performance must be reported separately for three run classes.

### Cold run

No valid deterministic artifacts for the input. This measures:

- OpenAI/LLM work;
- R2 upload/download;
- Salad allocation;
- image pull;
- model bootstrap/load;
- queue wait;
- GPU inference;
- Phase 9 render/mux.

It is the most important run for comparing cold-start overlap and shared-worker retention.

### Warm/cache run

All expensive deterministic R2/local artifacts are valid. This measures replay and orchestration
overhead. A healthy result should avoid model allocation for stages whose audit proves complete
cache coverage.

### Resume run

Start from an intentionally interrupted Phase 8 or partially completed stage manifest. This validates
that completed shots/stages are not regenerated and that known transports are reconciled rather than
duplicated.

## Real benchmark table

The end-to-end rows below intentionally remain unfilled until a real Salad invocation supplies the
evidence.

| Run | Historical/main baseline | Optimized PR | Saved | Improvement |
| --- | ---: | ---: | ---: | ---: |
| cold end-to-end | pending real run | pending real run | pending | pending |
| warm/cache | pending real run | pending real run | pending | pending |
| interrupted resume | pending real run | pending real run | pending | pending |

No percentage should be added to this table unless both compared values were actually measured under
documented conditions.

## Real validation command

After local/CI gates are green, the minimum real cloud validation for the optimized path is:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\run_video_factory.ps1 `
    -Input .\data\input\script.txt `
    -NonInteractive
```

That single command performs the fail-fast checks, executes the full DAG, renders `FinalVideo` and
runs final cleanup.

The operator should paste back the complete final summary plus these generated files:

```text
data/output/preflight_report.json
data/output/production_metrics.json
data/output/video_factory_metrics.json
```

and the controlled Salad metric lines emitted during the run. Those are sufficient to fill the
optimized benchmark row and inspect which startup segments remain on the critical path.

For a historical before/after claim, the baseline must be measured separately on the pinned original
SHA or from an already captured run using the same input/model/configuration. R2 must not be silently
warmed by the first side of the comparison.

## Cleanup acceptance criteria

Every real benchmark ends with:

```text
project workers stopped
replicas = 0
pending_change = false
queues have no pending/running jobs
```

`manage_salad_stack.ps1 -Action Stop` calls the zero-replica guard for every project service,
including FLUX. The guard waits for `status=stopped`, `replicas=0` and
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
