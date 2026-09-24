# Architecture overview

The production pipeline transforms a script into a rendered video through planning, narration,
alignment, visual generation, video generation, upscaling and composition.

## Pipeline stages

```text
script
  -> planning / shot structure
  -> narration (Breeze, Fish fallback)
  -> word alignment (Whisper)
  -> reference images / storyboard keyframes (Qwen Image)
  -> LTX 2.5 video clips
  -> Real-ESRGAN upscale
  -> Remotion / FFmpeg composition
  -> final output
```

OpenAI is used for language/planning tasks. GPU inference workers run on Salad.

## Application control plane

GPU inference has one application source of truth: Postgres `gpu.jobs`.

Providers build deterministic `InferenceJobRequest` objects and submit them through the Postgres
job transport. Workers poll Postgres, claim jobs with leases, download inputs from R2, perform
inference, upload outputs and complete the job.

R2 output metadata plus deterministic request fingerprints provide replay/idempotency before new GPU
work is allocated.

## Salad's role

Salad is a compute host, not a business-state or queue authority.

`deploy/salad/services.json` defines one container group per model, GPU requirements, probes,
priority and explicit replica bounds. Controlled pipeline wrappers inspect cache state before
starting capacity and stop groups back to zero when work finishes.

No Salad Job Queue or queue autoscaler is part of the production path.

## Worker design

All model services use the shared inference core for:

- health/readiness;
- task registration;
- Postgres claims, leases and heartbeat;
- object-storage integrity;
- replay;
- one-model-call-at-a-time process serialization.

Model-specific packages own bootstrap and inference implementation only.

Domain contracts do not contain Salad, R2 or Postgres transport details.

## Supported end-to-end entrypoint

The supported production entrypoint is:

```powershell
./scripts/pipeline/run_video_factory.ps1
```

It performs preflight checks, cache/resume planning, bounded GPU lifecycle, production DAG execution,
composition and final cleanup.
