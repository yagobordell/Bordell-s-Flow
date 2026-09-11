# Salad GPU profiles and parallel scaling

AI Video Factory deploys one model family per Salad container group and one queue per model family.
The queue is the unit of routing; container-group replicas are the unit of horizontal parallelism.
Each worker remains serialized internally so one GPU processes one inference job at a time.

## Initial production profiles

| Service | Model/profile | Initial GPU | Replica ceiling | Rationale |
| --- | --- | --- | ---: | --- |
| `whisper` | `openai/whisper-large-v3-turbo`, FP16 | RTX 3090 24 GB | 1 | Large VRAM margin and strong Salad supply without paying for Ada/Blackwell capacity that alignment does not require. |
| `breeze_tts2` | Breeze TTS 2 `--fast-all` | RTX 4090 24 GB | 2 | The fast path is documented around 14.4 GiB and recommends a 24 GB GPU. 4090 is the initial latency/throughput profile. |
| `ideogram4` | Ideogram 4 NF4 + `V4_QUALITY_48` | RTX 4090 24 GB | 4 | NF4 is the CUDA profile; 24 GB provides practical activation headroom for portrait/vertical Quality generations. |
| `ltx25` | LTX 2.5 distilled FP8-cast + CPU offload | RTX 5090 32 GB | 4 | The validated project benchmark peaked around 24.5 GiB, so a 24 GB class is not a safe production target. |

These are deployment baselines, not permanent hardware contracts. Every new worker should record a
real Salad smoke with latency and peak VRAM before a cheaper class replaces the baseline.

## GPU class resolution

`deploy/salad/services.json` stores `resources.gpu_class_names`, not provider UUIDs. Salad GPU-class
IDs are provider metadata and may be queried through the organization GPU-class endpoint.

During `Prepare`, `scripts/manage_salad_worker.ps1` resolves each configured name to the current Salad
class ID and submits those IDs in the container-group patch. Legacy `resources.gpu_classes` UUID arrays
remain accepted by the manager temporarily so an old service definition can still be prepared.

This means a service definition expresses the hardware intent directly:

```json
{
  "resources": {
    "gpu_class_names": ["RTX 4090"]
  }
}
```

rather than persisting an opaque UUID in source control.

## Parallelism policy

A worker process must not attempt to run concurrent model calls on one GPU. Parallelism is achieved by
queue autoscaling:

```text
model queue
  |-- replica 1 -> GPU 1 -> one job
  |-- replica 2 -> GPU 2 -> one job
  |-- replica 3 -> GPU 3 -> one job
  `-- replica 4 -> GPU 4 -> one job
```

This keeps model memory ownership simple and lets Postgres/R2 idempotency remain the coordination
boundary.

LTX and Ideogram are configured for up to four replicas. LTX fans independent shots across 5090s;
Ideogram fans both Phase 4 reference jobs and Phase 6 keyframe jobs across 4090s through one shared
model queue. Breeze is configured for up to two replicas, allowing two video narrations to synthesize
in parallel while keeping one resident fast-all runtime per 4090. Whisper remains at one replica by
default because alignment is comparatively lightweight and normally follows a single narration asset.

All services keep `min_replicas=0` so idle models scale to zero.

## Ideogram 4 scope

Both Phase 4 visual-reference images and Phase 6 storyboard keyframes use the dedicated Ideogram 4
worker with the open-weight NF4 model and `V4_QUALITY_48` sampler preset.

The open-weight Ideogram 4 pipeline is text-to-image. Its Qwen3-VL encoder is used in text-only mode,
and the released local pipeline does not expose the multiple reference-image conditioning previously
used by the OpenAI keyframe provider. Consequently, Phase 6 continuity is carried in the canonical
structured JSON caption: entity identity, clothing, physical traits, environment, palette, composition
and other persistent details are repeated explicitly in text.

`ReferenceAsset` remains persisted project evidence and a useful review artifact. The Phase 6
keyframe workflow no longer loads those PNGs or claims they are model inputs. Instead, OpenAI Luna
receives the relevant `VisualReference` captions and previous storyboard caption, then produces the
next structured Ideogram caption.

One Ideogram worker registers both `image.ideogram4.reference` and `image.ideogram4.keyframe`, so the
same resident model and the same autoscaled group serve both workloads without maintaining duplicate
24 GB model containers.

## Cost and benchmark policy

Hardware selection balances four factors:

1. model VRAM/headroom, including the fact that Salad/WSL may expose slightly less than nominal VRAM;
2. measured latency/throughput on the actual container;
3. current Salad supply for the GPU class;
4. current per-second price and preemption priority.

A cheaper GPU is promoted only after a representative smoke proves that it preserves output quality,
fits with safe memory headroom and improves cost per completed artifact.
