# LTX-2.5 real Salad validation — 2026-09-15

This checkpoint records the successful end-to-end validation of the dedicated production LTX-2.5
worker on real SaladCloud infrastructure after the Phase 8.3 worker was migrated to the shared
inference-worker architecture.

## Validated deployment

```text
service: ltx25
container group: ai-video-factory-ltx25-worker
queue: ai-video-factory-ltx25-jobs
GPU: RTX 5090 (32 GB)
priority: medium
group version: 5
cpu: 8
memory: 40960 MiB
shared memory: 8192 MiB
storage: 137438953472 bytes
autoscaler: min=0, max=4
image: docker.io/yagobordell/ai-video-factory@sha256:598d743b82f75e531cf29c521530a5b9d8d606a6d98a4e7b9fd737811c384a02
```

Before the smoke, `Prepare` completed with the group still
`stopped / replicas=0 / pending_change=False`. The final protected-smoke cleanup returned the group
to the same stopped, zero-replica state.

## Model bootstrap evidence

The dedicated image starts HTTP health before downloading the large LTX model set. The successful
instance downloaded all five required files with byte-level progress telemetry:

```text
diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors      42018190584 bytes
text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors             26263858182 bytes
vae/ltx-2.5-video-vae-bf16.safetensors                                   1472223346 bytes
vae/ltx-2.5-audio-vae-bf16.safetensors                                    364866540 bytes
latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors 995778752 bytes
```

For the successful instance, model download started at `09:07:51 UTC` and the worker reached
`ready=true` at `09:18:52 UTC`. Progress logs were emitted every 30 seconds and no
`MODEL_DOWNLOAD_STALLED` event occurred.

The production downloader retains two separate guards:

```text
progress interval: 30 seconds
stall timeout:     600 seconds without observed byte growth
```

The operator wrapper allows up to 45 minutes for a healthy cold bootstrap while keeping the
running/not-ready reallocation threshold at 60 minutes. This gives healthy large downloads time to
finish without weakening the 10-minute no-progress stall detector.

## Queue and readiness evidence

The worker reached readiness and started the Salad Job Queue transport:

```text
readiness changed ready=true
LTX-2.5 worker ready; starting Salad queue transport
```

The queue preflight did not list a container-group attachment, but the actual queue transport still
delivered the job to the worker. This confirms that the queue `container_groups` observation is not a
safe hard readiness gate for this stopped/scale-from-zero lifecycle.

During active inference, readiness temporarily changed from true to false and returned to true when
the job completed:

```text
09:18:58 UTC  received job
09:20:02 UTC  readiness changed ready=false
09:20:47 UTC  job completed
09:20:47 UTC  readiness changed ready=true
```

This is expected busy-worker behavior, not a runtime failure.

## Smoke result

The real protected smoke submitted one one-second Phase 8 video job through the dedicated queue:

```text
application_job_id: phase8-shot-001-730e00d82f95
salad_job_id:       e06633cb-814e-4e77-99c3-a2b5b0f2f9fd
task:               video.ltx25.generate
generation_profile: ltx25-distilled-a95ab856-fp8cpu-v1
resolution:         768x1280
fps:                24
num_frames:         25
seed:               43
replayed:           false
attempt_count:      5
status:             succeeded
```

The queue lifecycle reached `running` and then `succeeded`. The resulting MP4 was uploaded to R2,
downloaded by the orchestrator and verified:

```text
output_key: jobs/phase8-shot-001-730e00d82f95/shot_001.mp4
local artifact: data/output/deployment-validation/ltx25-cloud/shot_001.mp4
sha256: ff82d028b04bb5cf91a7198bf75f2e7cf6e585e1cbf25ca8679b376956dc655a
```

The smoke finished with:

```text
SMOKE_DONE status=succeeded
Phase 8.3 real video smoke test: OK
Real Salad smoke passed for: ltx25
```

The worker logs contained no `ERROR`, `Traceback`, `Exception`, or failed-job event for the successful
instance.

## Cost-safe operator path

The validated real-smoke command is:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_ltx25_protected_smoke.ps1 `
  -SkipLocalBuild
```

The wrapper starts the protected bootstrap, runs the real smoke only after one worker is ready, and
always calls the existing LTX stop path in `finally`.

The final cleanup reported:

```text
status=stopped
replicas=0
pending=False
```

No LTX GPU should remain active after this checkpoint.

## Follow-up optimization boundary

Correctness is now established for the current BF16-checkpoint + FP8-cast/CPU-offload runtime.
Separate optimization work may reduce cold-start cost, but it must not be mixed into this baseline.
Candidates include persistent/external model caching or a separately validated pre-quantized
checkpoint path. Any such change must rerun the same deterministic protected smoke and preserve the
zero-replica cleanup contract.
