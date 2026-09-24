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
