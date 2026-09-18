# FLUX.2 Klein 4B safety fallback

## Scope

FLUX.2 Klein 4B remains a safety-only fallback. Ideogram 4 is still the primary image provider for
Phase 4 reference assets and Phase 6 keyframes. The fallback is invoked only after the Ideogram
provider has exhausted its executable caption variants and returns a confirmed terminal safety
rejection. Transport errors, timeouts, bootstrap failures and ordinary inference errors do not
activate the fallback.

## Runtime identity

- Service: `flux2_klein`
- Model: `black-forest-labs/FLUX.2-klein-4B`
- Revision: `e7b7dc27f91deacad38e78976d1f2b499d76a294`
- Dtype: BF16
- Diffusers: 0.40.0
- Transformers: 5.17.0
- Salad group: `ai-video-factory-flux2-klein-worker`
- Salad queue: `ai-video-factory-flux2-klein-jobs`
- Docker tag: `flux2-klein-4b-bf16-diffusers040-v1`
- GPU: RTX 4090 24 GB
- CPU: 8
- RAM: 32768 MiB
- Storage: 96 GiB
- Autoscaling: min 0, max 1

BF16 is the default because the official Diffusers repository exposes `Flux2KleinPipeline`
directly and Black Forest Labs documents the 4B model for consumer GPUs with about 13 GB VRAM.
The official FP8 repository is a smaller single-file checkpoint, but it does not expose the same
ready-to-load Diffusers layout. Operational reliability takes precedence over the smaller download.

## Bootstrap

The bootstrap downloads only the Diffusers runtime components:

- `model_index.json`
- `scheduler/**`
- `text_encoder/**`
- `tokenizer/**`
- `transformer/**`
- `vae/**`

The duplicated top-level `flux-2-klein-4b.safetensors` file is intentionally excluded. The
snapshot is pinned to the immutable model revision above. Xet high-performance mode is enabled.

The shared model-download watchdog enforces:

- 600 seconds without byte progress before stall failure;
- 2400 seconds absolute download timeout;
- 15 second polling;
- 6 MiB/s minimum rolling throughput after the grace window;
- Salad IMDS reallocation request for slow/stalled nodes.

The HTTP process starts first. `/health` reports process health independently from model bootstrap.
`/ready` stays unavailable until the pinned snapshot marker exists and the BF16
`Flux2KleinPipeline` has loaded successfully.

## Inference contract

The distilled defaults are:

- `num_inference_steps=4`
- `guidance_scale=1.0`
- deterministic application seed derived from the application job ID;
- PNG output;
- width and height between 256 and 2048 and divisible by 16.

Phase 4 and Phase 6 keep their existing provider-neutral PNG contracts and output names. Fallback
metadata identifies `provider=flux2_klein`, the exact model ID, `fallback_from=ideogram4` and
`fallback_reason=safety_rejection`.

The application job ID includes the FLUX.2 generation profile and model ID. Therefore legacy
FLUX.1 Schnell R2 objects cannot become valid FLUX.2 cache hits. Existing Ideogram cache objects
remain reusable when their request fingerprints are unchanged.

## Lifecycle

Known safety-blocked Phase 4 work is prewarmed deterministically before queue submission. Phase 6
prewarms the fallback replica before generation. Dynamic Phase 4 safety rejections can still start
the scale-to-zero group without re-running Ideogram indefinitely.

After the batch, the controlled runners restore the manifest autoscaler, normalize desired replicas
to zero, stop the group and clean active queue work.

Useful commands:

```powershell
pwsh scripts/prepare_salad_worker_manifest.ps1 -Service flux2_klein -NonInteractive
pwsh scripts/start_salad_flux2_klein_prewarm.ps1 -NonInteractive
python scripts/run_salad_smoke_suite.py --service flux2_klein
pwsh scripts/restore_salad_flux2_klein_scale_to_zero.ps1 -NonInteractive
pwsh scripts/cleanup_salad_queue.ps1 -Service flux2_klein -NonInteractive
```
