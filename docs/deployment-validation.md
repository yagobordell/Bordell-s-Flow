# Real Salad deployment validation

This runbook validates the four model-specific Salad workers against real infrastructure before the
production runner is used for normal video generation.

The validation order is deliberately cost-conscious and dependency-aware:

```text
Breeze TTS 2  -> produces breeze-smoke.wav
Whisper       -> transcribes breeze-smoke.wav
Ideogram 4    -> validates both reference and keyframe task routing
LTX 2.5       -> animates the Ideogram keyframe
```

Only one model service needs to be started at a time. This keeps GPU spend controlled and isolates
failures to one image, queue, GPU class, or runtime.

## What the smoke suite proves

For each worker the smoke path uses the same queue, R2 storage, shared inference contracts and
provider adapter used by production. A successful smoke therefore validates more than a direct HTTP
health check.

- Breeze TTS 2: queue submission, scale-from-zero, resident runtime, WAV generation and R2 download.
- Whisper: R2 audio input, RTX 3090 worker, word-level timestamps and transcript JSON.
- Ideogram 4 Quality: both `image.ideogram4.reference` and `image.ideogram4.keyframe`, including
  1024x1536 production keyframe output.
- LTX 2.5: R2 keyframe input, production LTX task, MP4 generation and output SHA verification.

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

Completed real-infrastructure checkpoints are recorded separately so they remain useful after later
runbook changes:

```text
docs/breeze-salad-validation-2026-09-11.md
docs/whisper-salad-validation-2026-09-12.md
```

Breeze TTS 2 and Whisper have completed their independent queue-backed correctness baselines. Ideogram
4 and LTX 2.5 remain the next validation stages.

## Required local prerequisites

Before the first real deployment validation:

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

`SALAD_ORGANIZATION` and `SALAD_PROJECT` may be supplied in `.env`; the smoke client falls back to the
canonical values in `deploy/salad/services.json` when they are omitted.

## Operator command

All validation actions use:

```powershell
.\scripts\manage_salad_validation.ps1 -Service <service> -Action <action>
```

If the local PowerShell execution policy blocks project scripts, use a process-local bypass instead
of changing the machine policy:

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

Actions are intentionally separate:

```text
Validate
Prepare
Start
Status
Smoke
Stop
```

There is no one-command `Full` action. Building, starting paid GPU capacity, executing a smoke and
stopping it are separate operator decisions so the current state can be inspected between steps.

## Recommended first validation sequence

Run one worker at a time in this exact order.

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

Do not continue to Whisper if this stage fails because Whisper reuses this WAV as deterministic test
speech.

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

The production baseline passed on 2026-09-12 using an RTX 3090, priority `medium`, and image digest
`sha256:28ef8956398a646992dc1741ea6f9139dac93394697d11bd23f398ee8db41084`. The smoke returned ten
ordered words spanning `0.0` to `4.94` seconds. See
`docs/whisper-salad-validation-2026-09-12.md` for the full evidence and the bootstrap race fixed during
validation.

A later optimization pass may test a cheaper GPU, but do not change GPU class while validating another
variable at the same time.

### 3. Ideogram 4 Quality / RTX 4090

```powershell
.\scripts\manage_salad_validation.ps1 -Service ideogram4 -Action Validate
.\scripts\manage_salad_validation.ps1 -Service ideogram4 -Action Prepare
.\scripts\manage_salad_validation.ps1 -Service ideogram4 -Action Start
.\scripts\manage_salad_validation.ps1 -Service ideogram4 -Action Smoke
.\scripts\manage_salad_validation.ps1 -Service ideogram4 -Action Stop
```

The smoke submits two deterministic jobs through the same model queue: one Phase 4 reference task at
1024x1024 and one Phase 6 keyframe task at 1024x1536.

Acceptance criteria:

- both PNG files exist and are non-empty;
- the report identifies `V4_QUALITY_48`;
- both task names route successfully through the same resident worker image;
- the 4090 does not OOM during 1024x1536 Quality inference.

### 4. LTX 2.5 / RTX 5090

```powershell
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Validate
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Prepare
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Start
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Smoke
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Stop
```

The LTX smoke reuses `ideogram-keyframe.png`, constructs one 1-second synthetic shot and then calls
the canonical `submit_ltx25_smoke.py` path. The resulting MP4 must pass the existing R2 SHA check.

Acceptance criteria:

- exactly one non-empty `shot_001.mp4` is downloaded;
- output SHA matches worker metadata;
- the LTX worker succeeds on RTX 5090 without OOM;
- queue replay/idempotency remains visible in `ltx25-smoke.log`.

## Complete-chain smoke

After all four workers pass independently, all groups may be started with scale-to-zero and the smoke
suite can be executed as one chain:

```powershell
.\scripts\manage_salad_validation.ps1 -Service all -Action Start
.\scripts\manage_salad_validation.ps1 -Service all -Action Smoke
.\scripts\manage_salad_validation.ps1 -Service all -Action Stop
```

Because every group has `min_replicas=0`, starting the stack does not itself require a permanently
running GPU. Jobs cause the relevant groups to scale from zero.

## GPU optimization after correctness

The initial GPU assignments are validation baselines, not claims that each card is the cheapest
possible option:

```text
Whisper       RTX 3090 24 GB
Breeze TTS 2 RTX 4090 24 GB
Ideogram 4   RTX 4090 24 GB
LTX 2.5      RTX 5090 32 GB
```

Only change a GPU class after the baseline smoke passes. Re-run the exact same deterministic smoke
artifact after each class change and compare cold-start time, inference wall time, OOM behavior and
availability. This keeps GPU cost optimization separate from application correctness.

For Whisper, the first candidate for a cost-down experiment is a lower-cost 16-24 GB CUDA class if
Salad supply is good. Breeze and Ideogram should remain on 24 GB cards until measured headroom proves
otherwise. LTX should remain on 32 GB for now because the validated runtime has already operated near
the practical limit of a 24 GB card.

## Failure handling

If a smoke fails:

1. Run `Status` for that service.
2. Keep the other workers stopped.
3. Preserve `data/output/deployment-validation/` and the Salad container logs.
4. Run `Stop` for the failing service before changing image/runtime configuration.
5. Cancel any stale pending queue job created by the failed smoke before the next `Start`.
6. Fix one cause at a time, then repeat only that service's `Start` + `Smoke` cycle.

Do not proceed to the production runner until all four service reports show success and the final
complete-chain smoke has passed.
