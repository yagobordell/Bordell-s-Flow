# Whisper real Salad validation — 2026-09-12

This checkpoint records the first successful end-to-end validation of the production Whisper worker
on real SaladCloud infrastructure.

## Validated deployment

```text
service: whisper
container group: ai-video-factory-whisper-worker
queue: ai-video-factory-whisper-jobs
GPU: RTX 3090 (24 GB)
priority: medium
group version: 3
min replicas: 0
max replicas: 1
image: docker.io/yagobordell/ai-video-factory@sha256:28ef8956398a646992dc1741ea6f9139dac93394697d11bd23f398ee8db41084
```

The remote container environment was read back from the Salad API after `Prepare` and verified as:

```text
SALAD_QUEUE_ENABLED=true
INFERENCE_WORKER_MODE=production
WHISPER_DEVICE=cuda:0
WHISPER_DTYPE=float16
WHISPER_MODEL_ROOT=/workspace/models/whisper-large-v3-turbo
```

Before the successful smoke the queue was empty and the group was
`stopped / replicas=0 / pending_change=False`. The final `Stop` returned the group to the same state,
and the queue again reported `current_queue_length=0`.

## Issues resolved before the successful smoke

The validation uncovered several deployment issues that were fixed before the final run:

1. First deployment could reach `Prepare` with no existing queue or container group. The preflight queue
   repair now tolerates an absent queue so the worker manager can create the missing resources.
2. Fresh Salad container-group responses can omit optional `current_state.description`. The PowerShell
   manager now reads optional state fields safely under StrictMode.
3. The first published Whisper image exposed the model directory to the background runtime while
   Hugging Face was still downloading the model. Once `config.json` existed but the weights did not,
   Transformers attempted model construction and failed permanently before bootstrap completed.
4. The model downloader now downloads and validates the model in a staging directory, then promotes
   the completed directory into `WHISPER_MODEL_ROOT`. The runtime therefore sees either no model or a
   complete model, never a partially downloaded checkpoint.

The optional-state fix was merged in PR #83. The atomic model-bootstrap fix was merged in PR #84,
commit `0bee8ca31f4e97612b434a77174a9e619a9e74b3`.

A failed smoke left one pending queue job. It was cancelled before the corrected worker was started so
that the old request could not trigger an unexpected scale-from-zero event.

## Smoke result

The real queue-backed smoke reused the previously validated Breeze WAV and produced:

```text
data/output/deployment-validation/whisper-smoke.json
data/output/deployment-validation/smoke-summary.json
```

Recorded result:

```text
status: succeeded
service: whisper
model: openai/whisper-large-v3-turbo
wall_seconds: 1046.931
input: /workspace/data/output/deployment-validation/breeze-smoke.wav
word_count: 10
first_word_start_seconds: 0.0
last_word_end_seconds: 4.94
```

The transcript was:

```text
The AI video factory deployment smoke test is running successfully.
```

The ten word timestamps were ordered and contiguous from `0.0` through `4.94` seconds, matching the
validated Breeze source duration closely enough for the Phase 5 alignment contract.

The `wall_seconds` value includes the first real scale-from-zero cold start and model bootstrap, so it
is a correctness baseline rather than a steady-state inference benchmark.

## Safety / cost checkpoint

The final stop reported:

```text
status=stopped
replicas=0
pending=False
```

The queue then reported:

```text
ai-video-factory-whisper-jobs current_queue_length=0
```

No Whisper GPU should remain active after this checkpoint.

## Remaining hardening

The functional deployment baseline is complete. Separate follow-ups can optimize cost and startup
latency without repeating the correctness work above:

- pin an exact Hugging Face revision instead of `WHISPER_MODEL_REVISION=main`;
- measure warm inference latency and peak VRAM independently of cold-start bootstrap;
- evaluate a cheaper 16-24 GB CUDA class only after the remaining worker correctness baselines are
  complete;
- preserve the staging/promotion bootstrap contract for any future Whisper image changes.
