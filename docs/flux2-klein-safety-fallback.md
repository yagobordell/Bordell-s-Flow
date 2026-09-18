# FLUX.2 Klein 4B safety fallback

FLUX.2 Klein 4B is the image-generation fallback used only after Ideogram 4 reaches a confirmed terminal safety rejection. The primary Ideogram path and its retry/caption-variant behavior are unchanged.

## Runtime identity

- Service: `flux2_klein`
- Model: `black-forest-labs/FLUX.2-klein-4B`
- Revision: `e7b7dc27f91deacad38e78976d1f2b499d76a294`
- Precision: BF16
- Generation profile: `flux2-klein-4b-bf16-v1`
- Salad group: `ai-video-factory-flux2-klein-worker`
- Salad queue: `ai-video-factory-flux2-klein-jobs`
- Docker tag: `docker.io/yagobordell/ai-video-factory:flux2-klein-4b-bf16-v1`
- GPU: RTX 4090 24 GB
- CPU: 8
- RAM: 32768 MiB
- Storage: 96 GiB
- Autoscaler: min=0, max=1

Black Forest Labs documents the BF16 4B model at roughly 13 GB of VRAM and publishes a native Diffusers layout. The official FP8 repository is a smaller single-file checkpoint, but using it would require an additional single-file composition path instead of the model's documented Diffusers pipeline. On a 24 GB RTX 4090 the BF16 checkpoint leaves enough VRAM margin, so this deployment chooses the simpler official BF16 path for operational reliability.

## Inference contract

The distilled production checkpoint is guidance- and timestep-distilled. Requests are fixed to:

- `num_inference_steps=4`
- `guidance_scale=1.0`
- `max_sequence_length=512`
- width and height divisible by 16
- maximum 4 megapixels
- deterministic application seed derived from the application job ID

Phase 4 uses `image.flux2_klein.reference`; Phase 6 uses `image.flux2_klein.keyframe`. Output remains one PNG uploaded through the shared inference/R2 contract.

## Bootstrap

The container starts the HTTP application before downloading weights. Therefore `/health` can become healthy independently of the heavy model bootstrap, while `/ready` stays unavailable until the local snapshot is complete and `Flux2KleinPipeline` is resident.

The bootstrap downloads exactly one Diffusers snapshot using `hf download` + Xet. It includes `scheduler`, `text_encoder`, `tokenizer`, `transformer`, and `vae`, and deliberately excludes the top-level monolithic `flux-2-klein-4b.safetensors` so the transformer is not downloaded twice.

Download and runtime bootstrap both have bounded watchdogs. Slow/stalled downloads and stalled runtime stages can request Salad node reallocation.

## Safety fallback semantics

The fallback wrapper catches only a terminal Ideogram safety rejection. Other provider failures propagate normally. A terminal rejection invokes FLUX.2 Klein exactly once; it does not recursively re-run Ideogram.

Fallback metadata is provider-neutral at the workflow boundary and records:

- `provider=flux2_klein`
- `model=black-forest-labs/FLUX.2-klein-4B`
- the pinned model revision
- `fallback_from=ideogram4`
- `fallback_reason=safety_rejection`
- application job ID and request fingerprint

## Cache compatibility

Ideogram cache identities are unchanged and remain reusable.

FLUX fallback cache identities include generation profile, model ID and model revision. FLUX.1 Schnell fallback outputs therefore do not collide with or replay as FLUX.2 Klein outputs. Existing Phase 4/6 local filenames and URI conventions remain unchanged.

## Lifecycle

For a known fallback requirement, Phase 4/6 prewarm one ready FLUX.2 Klein replica before queue submission. Fresh Phase 4 Ideogram work can also arm the zero-replica fallback group in case a new terminal safety rejection is discovered at runtime.

Cleanup always restores the manifest autoscaler, normalizes desired replicas to zero, stops the group, and cleans active queue work.

Prepare a new image/configuration with:

```powershell
.\scripts\manage_salad_worker.ps1 -Action Prepare -Service flux2_klein -NonInteractive
```

Run the controlled real benchmark with:

```powershell
.\scripts\run_flux2_klein_benchmark.ps1 -NonInteractive
```

The benchmark records node assignment, Docker image pull, model bootstrap/time-to-ready, one real 1024x1024 queue generation with R2 upload/download, PNG size/hash, and verifies final stopped/zero-replica/empty-queue cleanup.
