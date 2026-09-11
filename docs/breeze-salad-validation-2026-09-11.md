# Breeze TTS 2 real Salad validation — 2026-09-11

This checkpoint records the first successful end-to-end validation of the production Breeze TTS 2
worker on real SaladCloud infrastructure.

## Validated deployment

```text
service: breeze_tts2
container group: ai-video-factory-breeze-tts2-worker-v2
queue: ai-video-factory-breeze-tts2-jobs
GPU: RTX 4090 (24 GB)
priority: high
group version: 6
min replicas: 0
max replicas: 2
image: docker.io/yagobordell/ai-video-factory@sha256:c83278ad2fcb3b6434e7120fd54ed277d8eea723a808a1c828192429f240e382
```

The remote container environment was read back from the Salad API after `Prepare` and verified as:

```text
SALAD_QUEUE_ENABLED=true
INFERENCE_WORKER_MODE=production
BREEZE_DEVICE=cuda:0
```

The group was stopped at `replicas=0` before `Start` and returned to `stopped / replicas=0` after the
smoke.

## Issues resolved before the successful smoke

The validation uncovered and fixed three independent deployment issues:

1. RTX 4090 availability for this workload was present at Salad priority `high`, not `medium`.
2. Salad requires `priority` inside the `container` object when updating an existing container group.
3. Local `.env` values such as `SALAD_QUEUE_ENABLED=false` and `INFERENCE_WORKER_MODE=local` were able
   to override production values declared in `deploy/salad/services.json` during `Prepare`.

The CUDA bootstrap also required `BREEZE_DEVICE=cuda:0` rather than the unindexed `cuda` value used in
the earlier deployment.

The environment-precedence fix was merged in PR #80, commit
`07c52fb415e4714e9be865116962a004a8ff157d`. `Prepare` then produced container group version 6 and the
three production environment values above were verified directly from `container.environment_variables`
before any new GPU smoke was started.

## Smoke result

The real queue-backed smoke completed successfully and produced:

```text
data/output/deployment-validation/breeze-smoke.wav
data/output/deployment-validation/breeze_tts2-smoke.json
data/output/deployment-validation/smoke-summary.json
```

Recorded result:

```text
status: succeeded
service: breeze_tts2
model: BreezeBlue/Breeze-TTS-2
wall_seconds: 978.352
size_bytes: 238124
sha256: bc9f0cb0ba8675aadb3247f9c93de532de693189faf31e849b9af2ec69cc97ab
duration_seconds: 4.96
sample_rate: 24000
channels: 1
text: The AI video factory deployment smoke test is running successfully.
```

A successful queue-backed smoke proves that the Salad Job Queue transport was active and able to move
the job through the production worker path. The exact startup log line was not retained as part of this
checkpoint, so this record does not claim that line as separate evidence.

## Safety / cost checkpoint

The final stop reported:

```text
status=stopped
replicas=0
pending=False
```

No Breeze GPU should remain active after this checkpoint.

## Remaining hardening

The functional deployment baseline is complete. The following are separate follow-ups and are not
required to repeat this smoke:

- pin the exact Breeze model checkpoint revision instead of `BREEZE_MODEL_REVISION=main`;
- measure peak VRAM and steady-state real-time factor explicitly;
- evaluate cost/latency optimization only after correctness baselines for the remaining workers are
  complete.
