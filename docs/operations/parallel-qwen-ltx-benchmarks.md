# Parallel Qwen and LTX benchmarks

## Current model

Qwen Image 2.1 and LTX 2.5 use separate Salad container groups and share the same Postgres/R2 control
plane.

Benchmark orchestration must preserve the production invariants:

- cache/R2 inspection before GPU allocation;
- Postgres as the only application job state;
- explicit replica capacity;
- immutable deployed image verification;
- one inference call at a time per GPU process;
- cleanup back to stopped `replicas=0`.

Qwen benchmarks normally request one RTX 5090 replica. LTX benchmarks may request multiple replicas,
bounded by `services.ltx25.capacity.max_replicas`.

## Preparation

Prepare both groups before a benchmark. Preparation leaves them stopped at zero replicas.

A legacy group that still exposes a Salad Job Queue attachment must be migrated by the normal
`Prepare` path. Migration is only allowed while stopped at zero replicas.

Do not create a queue, attach a queue, set a queue-autoscaler minimum or use a temporary prewarm mode.

## Running a benchmark

Start the exact capacity needed for the benchmark, then submit deterministic inference jobs through
the Postgres transport. Worker readiness and application completion are separate concerns:

1. Salad reports the requested replica capacity running.
2. The worker finishes model bootstrap and becomes ready.
3. The worker polls and claims the Postgres job.
4. The output is committed to R2 and the Postgres job becomes terminal.

Record Salad instance/GPU telemetry independently from the application job state.

## Cleanup

Stop each benchmark group after the run and require stable zero replicas. Cleanup does not cancel
jobs from a provider queue because no provider queue exists.

If a benchmark is interrupted, application recovery is driven by deterministic job identity,
Postgres leases and R2 replay.

## Historical incident: zero-to-one replica rebound

The previous architecture combined four actors:

- Postgres application state;
- Salad Job Queue state;
- Salad queue-autoscaler demand;
- local scripts mutating replicas and `min_replicas`.

On September 24, 2026, a real LTX run observed a group at
`stopped/replicas=0/pending_change=false` followed by
`stopped/replicas=1/pending_change=false`. At the same time the Salad queue summary reported work
that an exhaustive job listing did not show.

The evidence did not prove which Salad internal view was stale. It did demonstrate that local
orchestration could not establish a single authoritative demand state while both a remote
queue-autoscaler and local scripts were allowed to change capacity.

The current schema removes that ambiguity instead of adding more reconciliation logic:

```text
pipeline -> Postgres gpu.jobs -> inference workers
                         |
                         v
                 Salad compute capacity
```

No operational procedure should recreate the old queue-summary/list reconciliation, queue cleanup,
remote-minimum repair or warm-scale-out behavior.
