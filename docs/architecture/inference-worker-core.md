# Inference worker core

## Control plane

The application has one canonical inference job authority:

```text
Pipeline / provider
      |
      v
Postgres gpu.jobs
  - deterministic job_id
  - request fingerprint
  - pending/running/terminal state
  - leases and heartbeats
      |
      v
Inference worker on Salad
  - polls only supported task names
  - claims one job per GPU process
  - downloads inputs from R2
  - runs the model
  - uploads outputs to R2
  - completes the Postgres job
```

Salad is the execution host. It owns container groups, instances, GPU allocation and explicit replica
capacity. It is not an application queue and it does not decide whether an inference job exists.

## Shared core

The shared inference core owns:

- deterministic request fingerprints and application job identity;
- Postgres claims, leases, lease renewal and terminal state;
- replay/idempotency when an output already exists in R2;
- `/health`, `/ready` and `/jobs`;
- repository polling for the task names registered by the worker;
- one-model-call-at-a-time execution inside a worker process;
- task dispatch through `TaskRunnerRegistry`;
- object-storage input/output integrity.

The physical table is `gpu.jobs`. The name is retained for compatibility, but the table is the
canonical application queue for all interruptible inference work.

## Worker lifecycle

Production workers set:

- `INFERENCE_WORKER_MODE=production`
- `INFERENCE_WORKER_POLL_JOBS=true`
- `INFERENCE_WORKER_JOB_POLL_SECONDS=2`

A worker starts its HTTP process first so health probes remain responsive during model bootstrap.
Once the runtime is ready, a background poller asks the repository for the oldest reclaimable job
whose task is supported by that worker.

The worker then uses the normal claim/lease path before inference. An expired running lease is
reclaimable. Active leases remain exclusive. Terminal failures and cancellations are not silently
resubmitted.

The local execution lock prevents accidental concurrent inference inside one GPU process. Postgres
leases provide distributed exclusivity across replicas.

## Horizontal capacity

Each model has its own Salad container group. Horizontal parallelism comes from explicit replicas,
bounded by `capacity.max_replicas` in `deploy/salad/services.json`.

The pipeline checks R2/cache state before asking Salad for GPU capacity. Controlled wrappers then:

1. start the required number of replicas;
2. submit deterministic jobs to Postgres;
3. wait for application completion;
4. stop the group and converge to `replicas=0`.

No Salad Job Queue sidecar, queue attachment or queue autoscaler participates in this lifecycle.

## Configuration

Shared infrastructure configuration uses `INFERENCE_*`, `R2_*` and `POSTGRES_DSN`.
Model-specific settings stay under their model prefix.

`deploy/salad/services.json` is the deployment source of truth for:

- organization and project;
- container-group names;
- images and Dockerfiles;
- GPU classes and resource limits;
- probes and priority;
- explicit start/max replica capacity;
- production worker environment.
