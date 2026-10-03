# Salad stack deployment

## Source of truth

`deploy/salad/services.json` is the canonical deployment manifest.

Schema version 3 uses:

```json
{
  "stack": {
    "job_transport": "postgres"
  }
}
```

Account selection is **never** read from this manifest. Every operator must set
`SALAD_ORGANIZATION` and `SALAD_PROJECT` in their own `.env` (or process
environment), alongside `SALAD_API_KEY`. Non-secret application and Salad settings
live in `config/app.env` and `config/salad.env`. The PowerShell manager and Python
Capacity Controller load these files without overwriting existing process variables.
Account names must be URL-safe Salad
slugs. Missing or invalid account settings fail before any Salad API request.
`Validate` remains an offline schema check and does not require credentials.

`SALAD_QUEUE_NAME`, `SALAD_LOG_LEVEL` and `SALAD_QUEUE_ENABLED=false` are kept in
`config/salad.env` for compatibility. These settings
do not select the account or enable the removed Salad Job Queue; Postgres remains
the sole job transport. Do not turn `SALAD_QUEUE_ENABLED` on.

To run your own Salad organization without affecting production, use a separate
Postgres control plane and preferably a separate R2 bucket. Two independent Salad
accounts sharing the same `gpu.jobs` queue and Capacity Controller advisory lock
are **not** isolated merely by changing their API keys or account names.

Each model has its own unique Salad container group. The runtime rejects duplicate service-order
entries or duplicate `group_name` assignments before opening the Postgres control plane. Postgres is
the canonical job/control plane and R2 is shared object storage. Salad provides compute only.

Do not duplicate mutable image tags, GPU classes, group names or capacity ceilings in scripts or
documentation.

## Validate

Run the stack manager in validation mode before touching remote resources:

```powershell
./scripts/salad/manage_salad_stack.ps1 -Action Validate
```

Validation checks manifest schema, service names, capacity bounds, GPU-class declarations and local
Dockerfile paths. It does not allocate a GPU.

## Prepare

`Prepare` builds/pushes an image unless `-SkipBuild` is used, resolves the immutable digest, resolves
GPU class names through Salad's organization API, and creates or updates the container group while
leaving it stably stopped. A stopped group may still report a configured replica count; that value is
configuration for a future start, not effective running capacity.

A group created by the old architecture may still expose `queue_connection` or
`queue_autoscaler`. The manager never tries to partially patch those fields away. Migration is
allowed only when the group is stably stopped with no pending change; the manager then recreates the
group without queue state.

```powershell
./scripts/salad/manage_salad_worker.ps1 -Action Prepare -Service ltx25 -NonInteractive
```

Preparation verifies the activated immutable image, container priority, absence of legacy queue
attachments and a stable `stopped / pending_change=false` state.

## Start

`Start` sets an explicit replica count and starts the group. The requested count must not exceed
`capacity.max_replicas`.

```powershell
./scripts/salad/manage_salad_worker.ps1 -Action Start -Service ltx25 -Replicas 4 -NonInteractive
```

The command waits until the group is running, the change has settled and the observed replica count
matches the request.

There is no Salad Job Queue autoscaling. In production, the singleton Postgres-elected Capacity
Controller owns replica decisions. Explicit `Start`, `Stop` and `Prepare` acquire the same
session-scoped PostgreSQL advisory lock used by controller leadership and hold it for the full
mutation. The helper continuously verifies that its PostgreSQL session still owns that lock, while
the PowerShell manager performs a synchronous authority check before and after every Salad
`POST`, `PATCH` or `DELETE` and throughout long convergence waits. If the helper exits, the
database session is lost, or lock ownership disappears, the manual operation fails closed and emits
no further Salad mutations. A live controller therefore blocks manual capacity changes across
hosts, and a manual mutation blocks controller startup until it completes.
`-AllowControllerOverride` intentionally bypasses that coordination and is reserved for deliberate
operator intervention.

## Stop

`Stop` uses Salad's explicit stop operation and requires repeated stable
`stopped / pending_change=false` observations before returning success. It does not rewrite the
configured replica count merely to prove that capacity is off.

```powershell
./scripts/salad/manage_salad_worker.ps1 -Action Stop -Service ltx25 -NonInteractive
```

Normal production pipelines never run a project-wide stop. The global Capacity Controller observes
aggregate Postgres demand and owns scale-to-zero. Manual `Stop` remains an operator/smoke command.

## Status

`Status` reports the group state, explicit capacity bounds, image and whether a legacy queue
attachment is still visible.

Any `legacy_queue=True` result is a migration issue: stop the service until it is stable and run
`Prepare` before production use.

## Secrets and runtime configuration

The manager loads `.env` and the files in `config/` by default. Set `SALAD_ORGANIZATION` and
`SALAD_PROJECT` for every deployment; the manifest has no account fallback.
Required secrets such as `POSTGRES_DSN`, R2 credentials,
`HF_TOKEN` and `SALAD_API_KEY` are injected into the worker environment according to the manifest.

Production workers poll Postgres directly. Do not add `SALAD_QUEUE_ENABLED`, per-model queue names
or queue-autoscaler configuration back into deployment state.


## Concurrent LTX runs with different immutable Docker images

Apply `infra/sql/006_worker_image_routes.sql` to the shared Supabase/Postgres
database **before** deploying the new pipeline and controller. Restart the
singleton Capacity Controller after updating its code. All users must point
to the **same** PostgreSQL database, R2 bucket, Salad organization/project,
and the same running controller. Do not start one controller per computer.

Each user selects the exact pinned image through their own
`SALAD_LTX25_PAGEABLE_A2V_IMAGE` (A2V) and
`SALAD_LTX25_FP8_I2V_IMAGE` (I2V) environment settings. Both must reference
immutable digests under `docker.io/yagobordell/ai-video-factory`.

On `run.ps1`, the application saves a unique, deterministic image-scoped
job identity and registers the desired version in `gpu.worker_image_routes`.
It never starts, prepares or patches a shared LTX Docker image. The queue
task name itself includes the digest, so even an old unpatched worker that
still polls by base task name **cannot claim work from a new version**.
The controller discovers demand for each version, creates a **separate**
reviewed Salad Container Group when necessary, and manages replicas in the
same global project quota. Two users choosing the same digest share its
group; two different digests use different groups and queue partitions.
Existing unversioned jobs remain assigned exclusively to the original
fixed A2V/I2V groups until completed. **Never** enable multiple singleton
controllers for these groups.

The version-specific worker receives `INFERENCE_WORKER_IMAGE` matching its
immutable image identity. PostgreSQL pending-job selection and controller
demand filtering match that image exactly. Even direct worker requests with
a different image are rejected before any paid GPU inference begins.
A new group may need model bootstrap, but existing groups stay untouched;
new queued jobs wait for matching capacity. The existing zero-demand
downscale policy stops idle version groups. Registrations and stopped group
definitions remain for future reuse; no automatic deletion risks an active
or resumable job.

For the two-host migration:
1. Apply migration 006 once to the shared database.
2. Update and restart the singleton controller, keeping other capacity
   operations paused during its restart.
3. **Rebuild** each A2V/I2V Docker variant that will be used with `run`,
   including the routing-v1 worker adapter in its source. Older immutable
   images, including existing pre-migration digests, **do not** understand
   the new versioned task names and cannot be used as-is.
4. Smoke-test the exact rebuilt image and approve its immutable digest
   *once* in the shared database:

   ```powershell
   uv run --locked --extra dev python scripts/salad/approve_worker_image.py \`
     --service ltx25 \`
     --image "docker.io/yagobordell/ai-video-factory@sha256:<REBUILT_DIGEST>" \`
     --attest-routing-v1
   ```

   Use `--service ltx25_i2v` when approving an I2V variant. Never attest
   an old/unpatched image. Approval is centralized per digest and service:
   once approved, both hosts may use it without further coordination.
5. Update both users' local repositories. Set their intended A2V/I2V
   Docker digests independently. Different approved digests use different
   groups; identical approved digests share one group.
6. Run video generation normally. If the global GPU quota is exhausted,
   jobs remain queued rather than changing another user's Docker image.

An unapproved digest is registered but **rejected before paid inference**;
the controller will not allocate a GPU for it. A useful diagnostic tells
the user to rebuild, test and approve that version first. This avoids
spending money on an old container that appears ready but cannot claim
the new queue task. New version groups cannot read any legacy unversioned
jobs, and legacy groups cannot read image-scoped jobs.

Roll back safely by draining image-pinned pending/running jobs first. Old
workers intentionally cannot consume image-pinned jobs. Do not downgrade
only the controller or only a worker while versioned jobs are outstanding.

The existing `scripts/salad/manage_salad_worker.ps1` remains available for
operator-controlled maintenance of **legacy static groups** only. Do not use
it from `run`, and never patch an existing image-specific group to a
different digest. Keep `SALAD_VERSION_MAX_GROUPS` sufficiently large for the
authorized versions that must remain registered, while constraining the
global project replica quota to control GPU costs.


If you already have a manually managed experiment group in the *same* Salad
project, you may explicitly list its exact name in
`SALAD_EXTERNAL_GROUP_ALLOWLIST` (comma-separated). The controller never
changes allowlisted groups and reserves their observed running replicas from
the global project quota. During any external group's pending/deploying
transition, allocation is conservatively deferred until its capacity is known.
Unrecognized/unregistered groups still fail closed. This allowlist is for
legacy manual tests; ordinary `run` image versions need no allowlist.
