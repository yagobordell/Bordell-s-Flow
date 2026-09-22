# Unified Salad stack deployment

The four remote inference model families are deployed from one declarative manifest:
`deploy/salad/services.json`.

```text
AI Video Factory Salad stack
|
|-- whisper      -> RTX 3090 -> ai-video-factory-whisper-jobs
|-- breeze_tts2  -> RTX 4090 -> ai-video-factory-breeze-tts2-jobs
|-- ideogram4    -> RTX 4090 -> ai-video-factory-ideogram4-jobs
`-- ltx25        -> RTX 5090 -> ai-video-factory-ltx25-jobs
```

Each model has its own container group, image lifecycle and Job Queue. Postgres and R2 configuration
is shared. All groups keep queue autoscaling at `min_replicas=0`, so a started stack can remain at zero
GPU replicas while idle.

## Manifest schema

Schema v2 centralizes stack-wide properties:

- Salad organization and project;
- deterministic service operation order;
- shared Postgres/R2 environment requirements;
- the common `/jobs` worker path and port `8080`;
- autostart and restart policy.

Each service then declares only its model-specific image, Dockerfile, group, queue, GPU profile,
probes, autoscaler, environment defaults and additional required environment variables.

The current additional requirement is `HF_TOKEN` for Whisper downloads and the gated Ideogram and
LTX repositories.

## Commands

Validate the manifest and local Dockerfile paths without calling Salad or Docker:

```powershell
pwsh scripts/manage_salad_stack.ps1 -Action Validate
```

Build, push and prepare all four services:

```powershell
pwsh scripts/manage_salad_stack.ps1 -Action Prepare
```

`Prepare` performs these operations per service:

1. ensures the Job Queue exists;
2. builds and pushes the configured image unless `-SkipBuild` is supplied;
3. resolves the mutable image tag to an immutable registry digest;
4. resolves configured GPU names to current Salad GPU-class IDs;
5. creates the container group if it does not yet exist, otherwise patches it;
6. applies probes, queue connection, autoscaling and environment configuration;
7. verifies the deployed image and queue;
8. leaves the group stopped with zero replicas.

A fresh Salad project therefore does not require pre-created container-group slots.

Start the stack after it has been prepared:

```powershell
pwsh scripts/manage_salad_stack.ps1 -Action Start
```

Starting a group does not pin a GPU replica. Queue autoscaling remains `min_replicas=0`; replicas are
created when jobs arrive and are scaled down again when idle.

Inspect every service:

```powershell
pwsh scripts/manage_salad_stack.ps1 -Action Status
```

Stop every group. Stop runs in reverse service order:

```powershell
pwsh scripts/manage_salad_stack.ps1 -Action Stop
```

Operate only a subset when iterating on one or two model images:

```powershell
pwsh scripts/manage_salad_stack.ps1 `
  -Action Prepare `
  -Services ideogram4,ltx25
```

Use already-published image tags without rebuilding:

```powershell
pwsh scripts/manage_salad_stack.ps1 -Action Prepare -SkipBuild
```

For CI or another unattended shell, require all values to exist rather than prompting:

```powershell
pwsh scripts/manage_salad_stack.ps1 `
  -Action Prepare `
  -EnvFile .env `
  -NonInteractive
```

## Environment loading and secrets

Both stack and per-service managers load `.env` by default. The runtime environment declared in
`deploy/salad/services.json` is authoritative during `Prepare`; local `.env` values cannot disable
the Salad queue or change a worker from production to local mode. `.env` remains the source for
required secrets and external credentials. Values loaded or entered interactively are stored only in
the current process environment and are never written back to `.env` by these scripts.

`Prepare` needs:

```text
SALAD_API_KEY
POSTGRES_DSN
R2_ENDPOINT_URL
R2_BUCKET
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
HF_TOKEN            # only when selected services need it
```

`Start`, `Status` and `Stop` need only `SALAD_API_KEY` because the worker environment was persisted by
`Prepare`.

Anyone with sufficient access to the Salad container-group configuration may be able to inspect
container environment variables. Use narrowly scoped R2/Postgres/Hugging Face credentials and rotate
them if exposure is suspected.

## Per-service manager

The lower-level command remains useful when changing exactly one model:

```powershell
pwsh scripts/manage_salad_worker.ps1 -Service ideogram4 -Action Prepare
pwsh scripts/manage_salad_worker.ps1 -Service ideogram4 -Action Start
pwsh scripts/manage_salad_worker.ps1 -Service ideogram4 -Action Status
pwsh scripts/manage_salad_worker.ps1 -Service ideogram4 -Action Stop
```

`manage_phase8_worker.ps1` remains only as a deprecated LTX compatibility wrapper.

## Deployment safety

This repository change does not itself start paid GPU resources. `Prepare` leaves groups stopped and
`Start` must be requested explicitly.

The model revisions in the manifest are still `main` until each new model image completes its first
representative cloud smoke. After those smokes, pin each model revision to the validated immutable
upstream commit without changing the stack contract.

The next architecture block is local execution packaging: Dockerize the Python orchestrator and the
Remotion/FFmpeg renderer so the local control plane can run consistently while remote model workers
scale independently in Salad.
