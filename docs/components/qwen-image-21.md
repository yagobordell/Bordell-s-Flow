# Qwen Image 2.1 worker

Qwen is the active text-to-image generator for Phase 4 references and Phase 6 storyboard
keyframes. Production requests are prompt-only: reference-image conditioning is not
enabled. Each image is generated at **1280x736**, with 40 diffusion steps,
`true_cfg_scale=1` and KV caching. The output is a PNG without post-generation cropping.

## Deployed runtime

The Salad service `qwen_image_21` uses group
`ai-video-factory-qwen-image-21-worker-v2` on one RTX 5090 (32 GB).
The manifest pins the published image tag
`qwen-image-2.1-bf16-offload-1280x736-postgres-v7` and sets
`QWEN_IMAGE_21_MEMORY_MODE=bf16_offload`. Qwen's model revision is pinned by
the worker. Postgres `gpu.jobs` is the sole application job transport; R2 stores
the output artifacts. No Salad Job Queue or queue autoscaler is attached.

BF16 with CPU offload is the currently validated Qwen mode. The previous
bitsandbytes INT8 configuration returned a nearly transparent, visibly corrupted
image on a controlled test. A BF16 test with the same prompt and seed produced a
visually valid image and passed PNG and SHA-256 verification. That single run
establishes the recovery of this case, not a general quality or performance guarantee.
Do not reuse the known defective INT8 artifact as a keyframe or benchmark sample.

Worker-side output validation rejects an unexpected size/mode or a predominantly
transparent image **before publishing to R2**. The Qwen smoke also performs
this check after download. These checks detect the observed corruption but do not
replace a visual quality review; a previously cached artifact must be assessed
separately. Do not flatten a corrupted RGBA output to RGB to hide its alpha channel.

## Deployment and validation

After changing source code or the manifest, build and publish a **new image tag**
before applying it to Salad. A local `git pull` does not update a deployed worker.
`Prepare` must finish with `status=stopped`, `replicas=0`,
`pending=False`, and `legacy_queue=False`; verify the immutable image digest.
Only then submit a new Postgres-backed job and allocate explicit GPU capacity,
or use the production Capacity Controller. Stop manually allocated capacity after
a controlled test. Refer to `docs/operations/deployment-validation.md` for the
production acceptance criteria.

Phase 8 consumes the native 1280x736 keyframe without cropping. LTX fits the
image proportionally into 1280x720 and extends roughly 14 pixels per side using
edge pixels. LTX's internal 1280x768 model-grid padding is separate; the final
video remains 1280x720 for the existing upscale and compositor contracts.
