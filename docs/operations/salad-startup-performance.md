# Salad startup performance

Cold start is split into five observable stages:

1. Salad node allocation and image pull;
2. model download/bootstrap;
3. local model/pipeline construction;
4. worker readiness and Postgres polling;
5. inference.

Queue dispatch is not part of the Salad lifecycle.

## Allocate only after cache inspection

Controlled pipeline wrappers inspect R2/cache state before requesting GPU capacity. A complete replay
must never start a Salad replica.

When work remains, the wrapper calls `manage_salad_worker.ps1 -Action Start` with an explicit,
bounded replica count. On completion or failure it calls `Stop`.

Qwen, Whisper and speech fallback use one replica per controlled invocation. LTX and Real-ESRGAN may
request multiple replicas based on cache misses, capped by `capacity.max_replicas`.

## Model bootstrap

Workers expose `/health` before long model bootstrap so Salad can distinguish an alive process from
a ready model runtime. `/ready` becomes successful only after required model/runtime preparation.

Model download/watchdog logic remains model-specific. Network preflight may request Salad instance
reallocation when a node cannot sustain the required Hugging Face download path. This is a node
selection mechanism, not a job-state mechanism. LTX bootstrap emits
`LTX25_MODEL_BOOTSTRAP_METRIC`, and the I2V runtime separately emits pipeline-load and inference
metrics so network bootstrap is not conflated with local CUDA/model preparation.

Do not weaken checksum, revision, gated-model or required-file validation to improve startup time.

## Postgres polling

After readiness, the worker polls the canonical `gpu.jobs` table. It only considers registered task
names and processes one inference call at a time per GPU process.

The application job may already be `pending` before the worker becomes ready. That is intentional:
pending time now measures real capacity/bootstrap pressure rather than a separate Salad queue.

## Scale to zero

Scale-to-zero is explicit. There is no queue autoscaler and no remote `min_replicas` to restore.
The singleton Capacity Controller remains the only component allowed to stop or resize production
groups; workers never stop Salad themselves.

When a service has genuine zero remaining work, the controller may use the dedicated
`SALAD_AUTOSCALER_ZERO_DEMAND_STABLE_POLLS` window instead of the slower partial-downscale
stability window. It still publishes drain protection first. After a zero-demand drain is published,
the controller performs the next reconciliation after the drain grace when that is shorter than the
normal controller polling interval. Pending or retryable work prevents this fast path.

Cleanup succeeds only after the container group is stopped and replicas have remained zero across
multiple observations. A replica rebound after this architecture is therefore a direct Salad
container-group anomaly rather than a race between queue autoscaling and local scripts.

## Performance tuning

Prefer, in order:

1. R2 replay/cache hits that avoid GPU allocation entirely;
2. immutable images with stable dependency layering;
3. reliable model download paths and bounded watchdogs;
4. correct explicit parallel replica count for known outstanding work;
5. model/runtime optimizations.

For LTX investigations, retain the lifecycle log boundaries: model bootstrap, I2V pipeline load,
generic task start/end, durable artifact publication, autoscaler demand/remaining-work reason and
provider stop confirmation. The useful cost measurements are `running -> first useful inference`
and `last artifact/job completion -> stable stopped`, not `/ready` alone.

Do not reintroduce an additional queue or scaler solely to reduce cold-start latency.
