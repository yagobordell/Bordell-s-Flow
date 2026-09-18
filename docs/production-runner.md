# Production runner

The production runner is the control plane for a complete video. It reuses the validated phase
scripts and contracts; it does not implement a second copy of narrative, media or GPU business logic.

The canonical production input remains a finished source script. Phase 1 agents are experimental and
are intentionally outside this path.

## Normal command

On Windows PowerShell the normal production path is one command:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\run_video_factory.ps1 `
    -Input .\data\input\script.txt `
    -NonInteractive
```

The wrapper performs, in order:

```text
load .env
  -> install Remotion node_modules only when absent
  -> fail-fast local/network preflight
  -> Salad control-plane status preflight
  -> cache/resume-aware production DAG for phases 2-8
  -> Phase 9 composition + Remotion + FFmpeg
  -> FinalVideo validation
  -> unconditional project GPU cleanup
  -> queue cleanup
  -> wall-clock summary
```

The normal path does not require the user to start workers, wait for them manually, clean queues,
download clips, select a TTS provider or invoke Phase 9 separately.

Docker is not required by this command when the already-prepared Salad workers are being used.
Docker remains required for worker image build/prepare flows and for the optional local container
smokes. The historical `manage_local_stack.ps1 -Action Production` entry point now delegates to
`run_video_factory.ps1` on the host so it does not try to execute PowerShell GPU wrappers inside the
Python-only orchestrator image.

## Fail-fast preflight

`scripts/preflight_video_factory.py` runs before GPU allocation. The normal command checks:

- source input existence;
- `deploy/salad/services.json` schema and required model services;
- local `ffmpeg`, `ffprobe`, Node/npm and the Remotion CLI;
- required Salad credentials;
- R2 connectivity;
- the persistent Fish reference object, content type and exact SHA-256;
- Postgres connectivity with `SELECT 1`;
- access to the configured OpenAI model;
- authenticated Hugging Face access for model repositories declared by the Salad manifest.

After the Python preflight, `manage_salad_stack.ps1 -Action Status` verifies that the Salad control
plane is reachable for every configured service. A failure here happens before a worker is prewarmed.

The preflight has `--skip-network` only for CI/unit checks. Production does not use that option.

## Actual DAG

The previous runner iterated a linear list. The production runner now stores explicit dependencies
and schedules independent work concurrently with bounded resource limits:

```text
phase2-narrative
  |------------------------------|
  v                              v
phase3-continuity          phase5-narration [Breeze -> Fish]
  |------------|                  |
  v            v                  v
phase3-shots  phase4-reference-prompts   phase5-alignment [Whisper]
  |            |                  |
  |            +--> phase4-reference-assets [Ideogram]
  |                               |
  |                         phase5-beat-timing
  |                               |
  +----------------------- phase5-shot-timing
                                  |
                         phase6-storyboard
                           |             |
                           v             v
                 phase8-video-prompts  phase6-keyframes [Ideogram]
                           |             |
                           +------|------+
                                  v
                         phase8-videos [LTX-2.5]
                                  |
                                  v
                  Phase 9 Remotion + FFmpeg -> FinalVideo
```

`phase4-reference-assets` is still completed and persisted, but the current open Ideogram keyframe
path is text-conditioned. The binary Phase 4 PNGs are therefore not a false dependency of Phase 6
keyframe inference.

The scheduler defaults to at most four concurrent stages and two different GPU-backed stages. A
`resource_key` also serializes stages that share the same constrained model service. With the
current manifest, Phase 4 reference generation and Phase 6 keyframes cannot overlap on Ideogram.

These limits bound cost while allowing, for example, Breeze startup/inference to overlap continuity
planning and a different GPU service to overlap independent CPU/LLM work.

For regression isolation:

```bash
python scripts/run_production.py data/input/script.txt --serial
```

The individual `run_phaseX_*.py` and controlled PowerShell runners remain available for debugging.

## Shared-worker lifetime

End-to-end mode applies one deliberate warm-hold optimization:

1. Phase 4 audits its R2 cache before prewarm.
2. If fresh Ideogram work is required, the controlled wrapper prewarms one replica.
3. After successful Phase 4 generation, `-KeepIdeogramWarm` transfers cleanup ownership to the
   outer end-to-end orchestration.
4. The DAG resource key prevents Phase 6 from racing Phase 4 on the same service.
5. Phase 6 reuses the already-ready service if it needs Ideogram.
6. `-ReleaseSharedIdeogram` guarantees release after Phase 6 even when every keyframe is a cache
   hit.
7. The outer `finally` remains a second safety net and stops all project GPU services.

The switch is used only by `--end-to-end`. Standalone controlled Phase 4 keeps its historical
cleanup behavior.

Fish is explicitly excluded from speculative warm holding. It is a fallback and continues to use
zero GPU-seconds whenever Breeze succeeds.

## Cache before GPU

A cache hit is not based on local file existence alone.

### Phase 5 narration

Before Breeze prewarm, `audit_phase5_audio_cache.py` rebuilds the exact deterministic request from:

```text
text
voice
instructions
speed
cfg_scale
seed
model/generation profile
```

The R2 object must match application job ID, request SHA-256, content type, non-zero size and artifact
SHA metadata. A valid hit is materialized locally with no Breeze allocation and no Fish allocation.
Invalid metadata fails before GPU and is not classified as a TTS fallback condition.

### Phase 6 keyframes

`audit_phase6_keyframe_cache.py` evaluates each canonical Ideogram request and the persisted safety
rejection evidence before any image GPU prewarm.

Results are classified as:

```text
hit             -> replay; no GPU for that shot
miss            -> Ideogram work required
safety_blocked  -> FLUX fallback is deterministically required
invalid         -> fail before GPU
```

If fresh Ideogram work might discover a new safety rejection, FLUX is armed at scale-to-zero but is
not prewarmed. A FLUX GPU is prewarmed only when existing safety evidence already proves that the
fallback is needed.

### Phase 8 video

`audit_phase8_video_cache.py` rebuilds the exact LTX request for every shot before LTX prewarm.
`run_video_generation()` also performs the same verified R2 replay check before transport
reconciliation or submission. This gives two defenses:

- the controlled wrapper can avoid starting LTX entirely when every clip is already valid in R2;
- the workflow itself cannot submit a duplicate GPU job simply because the local manifest or local
  MP4 was lost.

A cached output with foreign/mismatched metadata is an error, not a hit.

## Resume and interrupted runs

The stage-level manifest remains:

```text
data/output/production_manifest.json
```

Schema version remains `2`. For every completed stage it stores the stage/script specification hash,
input hash, output hash, command and whether the artifacts were executed or adopted.

Scheduling-only fields such as DAG dependencies or resource limits are deliberately excluded from the
artifact specification hash. Changing orchestration must not invalidate semantically identical,
already-verified media.

Phase 8 additionally keeps `video_generation_manifest.json`, including application ID, request SHA,
transport ID/status, submission count and validated worker response per shot. A restart can:

```text
valid completed stage        -> skip
complete historical outputs  -> adopt
changed inputs/script        -> rerun only affected stage/downstream work
known pending/running job    -> reconcile transport
purged transport             -> resubmit same deterministic application ID
valid R2 output              -> replay before queue submission
failed/cancelled transport   -> explicit resumable retry
```

A failure on one shot does not require regenerating completed shots.

## Metrics

`scripts/run_production.py` writes:

```text
data/output/production_metrics.json
```

It records:

- measured phases 2-8 wall-clock;
- the sum of measured stage durations as a serial-equivalent reference;
- the measured DAG critical-path duration from the same run;
- overlap saved by concurrency;
- stage outcome (`executed`, `adopted`, `skipped`);
- stage wall-clock and resource class;
- configured stage/GPU concurrency limits.

The outer runner writes:

```text
data/output/video_factory_metrics.json
```

with total end-to-end wall-clock, phases 2-8 time, Phase 9 time, final path, zero manual intervention
for a successful run, and final cleanup state.

Cloud-specific model startup/inference metrics continue to come from the controlled Salad prewarm and
worker logs. They must be reported from a real cloud run; documentation must not invent them.

## Targeted inspection and reruns

Inspect persisted state without mutation:

```bash
python scripts/run_production.py data/input/script.txt --plan
```

Force one stage:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\run_video_factory.ps1 `
    -Input .\data\input\script.txt `
    -ForceStage phase6-keyframes `
    -NonInteractive
```

Stop the Python stage DAG after a stage for debugging:

```bash
python scripts/run_production.py data/input/script.txt --through phase6-keyframes
```

## Expected failure behavior

- missing credentials/configuration: fail during preflight, before GPU;
- malformed/stale cache metadata: fail before GPU rather than silently regenerate;
- application/R2/Postgres/input bugs: propagate as failures, not fallback;
- Breeze eligible operational/model failures: the existing classified Fish fallback policy applies;
- upstream DAG failure: dependent stages are never scheduled;
- concurrently running controlled stages finish their own cleanup before the runner exits;
- outer end-to-end cleanup then stops project services, verifies zero replicas and cleans queues.

## Local/cloud boundary

The normal control flow now runs on the Windows host because the controlled Salad lifecycle scripts
are PowerShell scripts. Model inference remains remote:

```text
Windows host orchestrator
  Python DAG + PowerShell lifecycle
  OpenAI structured planning
  R2/Postgres/Salad clients
          |
          v
Salad Job Queues
          |
          v
model-specific Salad workers
  Breeze TTS 2  -> RTX 4090 primary
  Fish Speech   -> RTX 4090 fallback only, max 1
  Whisper       -> configured worker GPU
  Ideogram 4    -> RTX 4090 shared Phase 4/6
  FLUX.2 Klein  -> safety fallback
  LTX-2.5       -> RTX 5090
          |
          v
persisted data/output
          |
          v
local Remotion + FFmpeg
          |
          v
FinalVideo
```

The Compose orchestrator/renderer images are retained for isolation tests and dedicated local render
workflows, but they are no longer required to bridge the complete production control plane.
