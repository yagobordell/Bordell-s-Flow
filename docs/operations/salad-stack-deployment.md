# Salad stack deployment

`deploy/salad/services.json` is the source of truth for Salad deployment.

The current service inventory is:

```text
whisper
breeze_tts2
fish_speech
ideogram4
qwen_image_21
ltx25
realesrgan
```

Each model has its own container group and queue. Postgres/R2 infrastructure is shared. Service
definitions declare image, Dockerfile, resources, probes, autoscaling, model environment and any
additional required credentials.

Do not duplicate mutable GPU classes, replica ceilings, queue names or image tags in documentation;
read them from the manifest.

## Stack commands

Validate the manifest and local paths:

```powershell
pwsh scripts/salad/manage_salad_stack.ps1 -Action Validate
```

Prepare all services:

```powershell
pwsh scripts/salad/manage_salad_stack.ps1 -Action Prepare
```

Operate the stack:

```powershell
pwsh scripts/salad/manage_salad_stack.ps1 -Action Start
pwsh scripts/salad/manage_salad_stack.ps1 -Action Status
pwsh scripts/salad/manage_salad_stack.ps1 -Action Stop
```

Use `-Services` to target a subset and `-SkipBuild` when immutable images are already available.

The lower-level single-service manager is:

```powershell
pwsh scripts/salad/manage_salad_worker.ps1 -Service ltx25 -Action Prepare
```

## Prepare semantics

`Prepare` ensures the queue exists, builds/pushes the image when requested, resolves its immutable
digest, resolves configured GPU names to Salad class IDs, creates or patches the container group,
applies probes/autoscaling/environment and leaves the service stopped.

Runtime values declared in the manifest are authoritative. Local `.env` supplies required secrets
and external credentials; it must not override deployment-managed worker mode or queue policy.

## Scale to zero

Production services use queue autoscaling with `min_replicas=0`. A started group may therefore remain
in Salad's `deploying` state with zero replicas and no pending change until a queued job triggers
allocation. The management scripts treat that as a valid active scale-to-zero state.

Always stop paid services after validation and verify zero replicas.

## Safety

Repository changes and `Prepare` do not intentionally keep paid GPUs running. Use `Start` only when
work is ready to submit, and rely on controlled wrappers for normal pipeline execution.
