# Qwen Image 2.1 worker

Qwen is the active text-to-image generator for Phase 4 references and Phase 6 storyboard
keyframes. The production artifact is a native **1280x736 PNG**, with no post-generation
crop. This near-16:9 size is divisible by 32 but is not exact 16:9. The primary request
contract is prompt-only; binary reference conditioning is not enabled in the current worker.

The pinned production profile keeps 40 inference steps, true CFG 1 and KV cache. Salad
uses the RTX 5090 with explicit `QWEN_IMAGE_21_MEMORY_MODE=int8_cuda`. Before applying the
updated Salad manifest, build and publish the new Qwen and LTX Docker image tags. A configuration
or source commit alone does not update a running worker.

Phase 8 accepts the 1280x736 keyframe without pre-cropping. LTX preserves all image content
by proportionally fitting it to 1280x720 and extending approximately 14 pixels on each side
with edge pixels; the LTX internal 1280x768 model-grid padding is separate. Final video
remains 1280x720 for the existing 2x upscale and compositor contracts.

## Controlled INT8 benchmark

The five-run benchmark uses the explicit `qwen-image-2.1-1536x864-40step-kv-benchmark-v1`
request profile. It does not change production's 1280x736 image contract or its
prompt-only provider. Use `scripts/smoke/benchmark_qwen_image_21.ps1 -PromptFile <path>`
with the **exact** historical prompt as a UTF-8 file. The runner builds and publishes the
manifest image, protects one RTX 5090 instance, submits five distinct application jobs
with identical prompt and seed, and stops the worker in `finally`. Run 1 warms CUDA;
runs 2-5 supply the generation-total median/mean/min/max and separate inference/PNG-save statistics. Each run writes PNG and worker metrics
(inference, PNG save, total, peak CUDA allocated/reserved memory, worker identity).
Model download and runtime initialization are operational timings, not inference.
A same-instance check and a per-run worker process ID guard prevent silent comparisons
between separate container starts. The four-run generation-total target is median <=30 s;
30-35 s is initially acceptable without OOM, and >35 s or >20% spread requires review.
A separate production-resolution measurement should use 1280x736; never use this
benchmark profile in normal Phase 4/6 requests.

### Benchmark Salad control-plane preflight

The five-run PowerShell benchmark first invokes the exact Python Salad instances
API client with `--preflight-only`, before deploying, starting or paying for a
worker. It does not submit a job or access R2. The Python client explicitly
identifies itself with the same user agent as the successful protected PowerShell
bootstrap; Python's default `Python-urllib/X.Y` client identity can be rejected
by API gateways even when PowerShell can access the same endpoint. The official
Salad API documents GET `/organizations/{organization}/projects/{project}/containers/{group}/instances`
with the `Salad-Api-Key` header.

If the preflight reports HTTP 403, inspect the API key's scope and Python's
network/proxy policy; do not repeatedly allocate a GPU for a control-plane
authentication problem. The runner still verifies the same started Salad instance
before and after each job and validates the worker process and pipeline across all
five generations. This is a local benchmark-client change only: the deployed
Qwen worker image and INT8 generation settings do not need to change.


### Warm five-run readiness regression (2026-09-24)

The v4 RTX 5090 worker reached `ready=True` and received benchmark job 1.
The benchmark downloaded and verified the PNG and its R2 metadata, but
immediately afterward Salad's instances endpoint temporarily reported no
`started && ready` instance, so the old client discarded a completed warmup.
Its `ready()` held the same mutex as the complete 40-step `generate()`
call; concurrent HTTP readiness probes could therefore time out while the
GPU was busy. The captured CSV contains the job receipt and many repeated
bitsandbytes dtype-cast warnings, but its 1,000-row export ends before the
later readiness transition; these warnings alone do not establish a GPU
failure or out-of-memory condition.

Qwen worker v5 keeps the inference lock for pipeline creation and each
generation while checking already prepared state and the immutable model
snapshot **without acquiring that lock** in `ready()`. The five-run client
now waits at most 60 seconds for a transient ready=false to recover, records
its last observation on timeout and rejects a changed instance or machine
immediately; it still requires the exact original instance before and after
every job and checks worker process/pipeline identity through the metadata.
Never interpret a replaced or restarted worker as a valid warm benchmark.

The v5 image addressed the earlier readiness regression, but the deployed v5 digest
predates the Postgres polling refactor. Its legacy startup banner
`queue transport disabled` indicates the old Salad queue consumer is disabled;
it does not provide the Postgres job poller required by the current smoke test.
Build and publish `qwen-image-2.1-int8-1280x736-postgres-v6` from the
current branch before running Qwen Prepare, then confirm the new immutable
digest is active while the group remains stopped at zero replicas.
A local `git pull` does not update container contents. Do not reuse the
older v4/v5 digests for a Postgres-backed deployment. The local API `--preflight-only` protection from the 403
incident remains active. CI covers nonblocking readiness and identity
recovery; live Salad five-run performance and visual quality remain to
be measured.

### BF16 diagnostic after the invalid v6 PNG (2026-09-25)

A real Postgres-backed v6 smoke succeeded in one attempt, but the stored 1280x736 PNG
was largely purple and transparent (approximately 96% of pixels had alpha <=16).
R2 SHA-256, format, dimensions, and transport success did not establish image quality.
The original output remains stored and must not be used as an LTX keyframe.

The suspect INT8 path quantized both the transformer and text encoder. To isolate
that variable, the recovery branch stages
`qwen-image-2.1-bf16-offload-1280x736-postgres-v7` with
`QWEN_IMAGE_21_MEMORY_MODE=bf16_offload`. This is a diagnostic configuration,
not yet a verified quality or performance improvement. The 40-step, 1280x736,
guidance-free, KV-cache profile and the Qwen checkpoint are unchanged.

The worker now rejects nearly transparent production output before R2 publication;
the normal Qwen smoke also performs this check. This catches the observed
failure, but cannot replace a visual quality review. Never flatten a nearly
transparent RGBA frame to RGB to conceal the failure.

From a clean recovery branch with Docker running and the group stopped at zero:

```powershell
pwsh -NoProfile -File .\scripts\salad\manage_salad_worker.ps1 -Service qwen_image_21 -Action Prepare -NonInteractive
pwsh -NoProfile -File .\scripts\smoke\run_qwen_bf16_recovery_controlled.ps1 -SourceJobId <previous-succeeded-qwen-keyframe-job-id> -NonInteractive
```

The controlled test preflights R2 and Postgres before allocating a GPU, verifies
that Salad's stopped group uses the published BF16 digest, and rejects other
active Qwen jobs. It copies the original prompt, seed, resolution and diffusion
parameters to a **new job ID and R2 output key** with a single permitted attempt,
then validates the returned PNG. A PowerShell `finally` block stops Qwen and
returns the service to zero replicas even if the test fails. The test image and
report are saved to `data/output/qwen-bf16-recovery/`.

Do not merge the recovery PR or promote BF16 to a production default until the
new image passes visual inspection. If BF16 produces the same defect, investigate
latent statistics and the VAE decode rather than publishing another untested
quantization preset. The previous five-run INT8 benchmark is not a BF16 quality
acceptance test and should not be run with the diagnostic image.
