# Real Salad deployment validation

This runbook validates the four model-specific Salad workers against real infrastructure before the production runner is used for normal video generation.

The validation order is deliberately cost-conscious and dependency-aware:

```text
Breeze TTS 2  -> produces breeze-smoke.wav
Whisper       -> transcribes breeze-smoke.wav
Ideogram 4    -> validates both reference and keyframe task routing
LTX 2.5       -> animates the Ideogram keyframe
```

Only one model service needs to be started at a time. This keeps GPU spend controlled and isolates failures to one image, queue, GPU class, or runtime.

## What the smoke suite proves

For each worker the smoke path uses the same queue, R2 storage, shared inference contracts and provider adapter used by production.

- Breeze TTS 2: queue submission, scale-from-zero, resident runtime, WAV generation and R2 download.
- Whisper: R2 audio input, RTX 3090 worker, word-level timestamps and transcript JSON.
- Ideogram 4 Quality: both `image.ideogram4.reference` and `image.ideogram4.keyframe`, including 1024x1536 production keyframe output.
- LTX 2.5: R2 keyframe input, dedicated LTX queue/worker, direct production LTX task, MP4 generation, output SHA verification and protected zero-replica cleanup.

Evidence is stored under:

```text
data/output/deployment-validation/
```

Expected evidence includes:

```text
breeze-smoke.wav
breeze_tts2-smoke.json
whisper-smoke.json
ideogram-reference.png
ideogram-keyframe.png
ideogram4-smoke.json
ltx25-cloud/shot_001.mp4
ltx25-smoke.log
ltx25-smoke.json
smoke-summary.json
```

Completed real-infrastructure checkpoints are recorded separately:

```text
docs/breeze-salad-validation-2026-09-11.md
docs/whisper-salad-validation-2026-09-12.md
docs/ltx25-salad-validation-2026-09-15.md
```

Breeze TTS 2, Whisper and LTX 2.5 have completed their independent queue-backed correctness baselines. Ideogram 4 remains a separate validation stage unless its own dated checkpoint is present.

## Required local prerequisites

Before real deployment validation:

1. Docker Desktop must be running with Docker Compose v2.
2. Docker must be authenticated to the registry used by `deploy/salad/services.json`.
3. `.env` must contain the Salad API key, Postgres DSN and R2 credentials.
4. `HF_TOKEN` must be present for LTX 2.5 and Ideogram 4 deployment.
5. The Hugging Face account behind `HF_TOKEN` must have accepted any gated Ideogram repository terms.

The shared required environment is:

```text
SALAD_API_KEY
POSTGRES_DSN
R2_ENDPOINT_URL
R2_BUCKET
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
HF_TOKEN
```

`SALAD_ORGANIZATION` and `SALAD_PROJECT` may be supplied in `.env`; the smoke client falls back to the canonical values in `deploy/salad/services.json` when they are omitted.

## Generic operator command

Normal validation actions use:

```powershell
.\scripts\manage_salad_validation.ps1 -Service <service> -Action <action>
```

If local execution policy blocks project scripts, use a process-local bypass instead of changing the machine policy:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\manage_salad_validation.ps1 `
  -Service <service> `
  -Action <action>
```

Services are:

```text
breeze_tts2
whisper
ideogram4
ltx25
all
```

The generic actions remain:

```text
Validate
Prepare
Start
Status
Smoke
Stop
```

For LTX's expensive cold-start smoke, however, use the dedicated protected wrapper documented below rather than manually chaining `Start` and `Smoke`.

## Recommended first validation sequence

### 1. Breeze TTS 2 / RTX 4090

```powershell
.\scripts\manage_salad_validation.ps1 -Service breeze_tts2 -Action Validate
.\scripts\manage_salad_validation.ps1 -Service breeze_tts2 -Action Prepare
.\scripts\manage_salad_validation.ps1 -Service breeze_tts2 -Action Start
.\scripts\manage_salad_validation.ps1 -Service breeze_tts2 -Action Smoke
.\scripts\manage_salad_validation.ps1 -Service breeze_tts2 -Action Stop
```

Acceptance criteria:

- `breeze-smoke.wav` exists and is non-empty;
- its report contains a positive duration and sample rate;
- the Salad job succeeds after scale-from-zero;
- the group returns to stopped state after the final command.

Do not continue to Whisper if this stage fails because Whisper reuses this WAV as deterministic test speech.

### 2. Whisper / RTX 3090

```powershell
.\scripts\manage_salad_validation.ps1 -Service whisper -Action Validate
.\scripts\manage_salad_validation.ps1 -Service whisper -Action Prepare
.\scripts\manage_salad_validation.ps1 -Service whisper -Action Start
.\scripts\manage_salad_validation.ps1 -Service whisper -Action Smoke
.\scripts\manage_salad_validation.ps1 -Service whisper -Action Stop
```

Acceptance criteria:

- `whisper-smoke.json` reports `status=succeeded`;
- at least one word with valid start/end timestamps is returned;
- the assigned GPU class is RTX 3090;
- there are no CUDA OOM or model-loading errors;
- the final queue length returns to zero and the group is stopped at zero replicas.

The production baseline passed on 2026-09-12 using an RTX 3090, priority `medium`, and image digest `sha256:28ef8956398a646992dc1741ea6f9139dac93394697d11bd23f398ee8db41084`. See `docs/whisper-salad-validation-2026-09-12.md` for full evidence.

### 3. Ideogram 4 Quality / RTX 4090

```powershell
.\scripts\manage_salad_validation.ps1 -Service ideogram4 -Action Validate
.\scripts\manage_salad_validation.ps1 -Service ideogram4 -Action Prepare
.\scripts\manage_salad_validation.ps1 -Service ideogram4 -Action Start
.\scripts\manage_salad_validation.ps1 -Service ideogram4 -Action Smoke
.\scripts\manage_salad_validation.ps1 -Service ideogram4 -Action Stop
```

The smoke submits two deterministic jobs through the same model queue: one Phase 4 reference task at 1024x1024 and one Phase 6 keyframe task at 1024x1536.

Acceptance criteria:

- both PNG files exist and are non-empty;
- the report identifies `V4_QUALITY_48`;
- both task names route successfully through the same resident worker image;
- the 4090 does not OOM during 1024x1536 Quality inference.

### 4. LTX 2.5 / RTX 5090 — protected path

First validate and, only when intentionally publishing a new worker image/configuration, prepare the stopped group:

```powershell
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Validate
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Prepare
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Status
```

`Prepare` must finish with `stopped / replicas=0 / pending_change=False`. If the already deployed immutable digest and Salad configuration are still the intended baseline, do not rebuild merely to repeat a smoke.

Build the local orchestrator before starting paid GPU time, then use the cost-guarded wrapper:

```powershell
docker compose build orchestrator

powershell -ExecutionPolicy Bypass -File scripts/run_ltx25_protected_smoke.ps1 `
  -SkipLocalBuild
```

Do **not** replace this with the old manual `Start` + `Smoke` sequence for correctness validation. The wrapper:

- allows up to 45 minutes for a healthy cold bootstrap;
- prevents running/not-ready reallocation before a 60-minute threshold;
- keeps the model downloader's 600-second no-byte-progress stall watchdog;
- runs the real LTX smoke only after one worker is ready;
- calls the existing LTX `Stop` path in `finally`.

The LTX smoke reuses the deployment-validation keyframe, constructs one one-second synthetic shot and calls the canonical `submit_ltx25_smoke.py` path.

Acceptance criteria:

- all five required model files reach `MODEL_DOWNLOAD_DONE` or are already present;
- one worker reaches `ready=true`;
- the Job Queue delivers the request even if `queue.container_groups` attachment observation is absent;
- the job advances through `running` to `succeeded`;
- exactly one non-empty `shot_001.mp4` is downloaded;
- output SHA matches worker metadata;
- the RTX 5090 worker completes without OOM or runtime exception;
- the wrapper finishes with the group `stopped / replicas=0 / pending=False`.

The production baseline passed on 2026-09-15 with:

```text
group:   ai-video-factory-ltx25-worker
queue:   ai-video-factory-ltx25-jobs
version: 5
image:   docker.io/yagobordell/ai-video-factory@sha256:598d743b82f75e531cf29c521530a5b9d8d606a6d98a4e7b9fd737811c384a02
job:     e06633cb-814e-4e77-99c3-a2b5b0f2f9fd
sha256:  ff82d028b04bb5cf91a7198bf75f2e7cf6e585e1cbf25ca8679b376956dc655a
```

See `docs/ltx25-salad-validation-2026-09-15.md` for full evidence.

## Complete-chain smoke

After every required worker has passed independently, groups may be enabled with scale-to-zero and the smoke suite can be executed as one chain:

```powershell
.\scripts\manage_salad_validation.ps1 -Service all -Action Start
.\scripts\manage_salad_validation.ps1 -Service all -Action Smoke
.\scripts\manage_salad_validation.ps1 -Service all -Action Stop
```

Because each group has `min_replicas=0`, starting the stack does not itself require a permanently running GPU. Jobs cause the relevant groups to scale from zero. For an isolated LTX correctness revalidation, prefer the dedicated protected wrapper above.

## GPU optimization after correctness

The current correctness baselines are not claims that each card or checkpoint format is the cheapest possible option:

```text
Whisper       RTX 3090 24 GB
Breeze TTS 2 RTX 4090 24 GB
Ideogram 4   RTX 4090 24 GB
LTX 2.5      RTX 5090 32 GB
```

Only change one cost variable after baseline correctness has passed. Re-run the same deterministic smoke artifact and compare cold-start time, inference wall time, OOM behavior and availability.

For LTX, future candidates include external/persistent model caching or a separately validated pre-quantized checkpoint path. Do not mix those changes into the current BF16-checkpoint + FP8-cast/CPU-offload correctness baseline.

## Failure handling

If a smoke fails:

1. Run `Status` for that service.
2. Keep the other workers stopped.
3. Preserve `data/output/deployment-validation/` and the Salad container logs.
4. Run `Stop` for the failing service before changing image/runtime configuration, unless the protected LTX wrapper already reports successful cleanup.
5. Cancel any stale pending queue job created by the failed smoke before the next start.
6. Fix one cause at a time, then repeat only that service's validation cycle.

For LTX, distinguish a slow but healthy download from a true stall using `MODEL_DOWNLOAD_PROGRESS`, `observed_bytes`, `delta_bytes` and `idle_seconds`; do not lengthen timeouts solely because a large checkpoint takes time to transfer.
