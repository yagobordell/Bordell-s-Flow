# Ideogram 4 Quality worker

AI Video Factory uses one dedicated Ideogram 4 container group for both Phase 4 visual-reference
images and Phase 6 storyboard keyframes. The worker runs the open-weight NF4 model with the
`V4_QUALITY_48` sampler preset and does not call Ideogram's hosted magic-prompt API.

## Service boundary

```text
OpenAI Luna (semantic visual planning)
        |
        | structured Ideogram JSON captions
        v
ai-video-factory-ideogram4-jobs
        |
        +--> image.ideogram4.reference  -> Phase 4 PNG
        |
        `--> image.ideogram4.keyframe   -> Phase 6 PNG
        |
        v
Ideogram 4 NF4 / V4_QUALITY_48
RTX 4090, one active inference per replica
        |
        v
R2 + Postgres replay/idempotency
```

The model image registers both tasks against the same resident pipeline. It remains serialized inside
one process/GPU. Horizontal throughput comes from Salad autoscaling the group from zero to four
independent RTX 4090 replicas.

## Structured caption contract

The local Ideogram 4 release is trained for structured JSON captions. AI Video Factory therefore does
not submit plain text and does not enable Ideogram magic prompt. OpenAI Luna produces provider-aware
visual plans and the application serializes them into the official key structure:

```json
{
  "high_level_description": "...",
  "style_description": {
    "aesthetics": "...",
    "lighting": "...",
    "photo": "...",
    "medium": "...",
    "color_palette": ["#RRGGBB"]
  },
  "compositional_deconstruction": {
    "background": "...",
    "elements": [
      {
        "type": "obj",
        "bbox": [0, 0, 1000, 1000],
        "desc": "..."
      }
    ]
  }
}
```

`art_style` replaces `photo` for non-photographic projects. Text elements are deliberately excluded
because the video pipeline does not want captions, labels, watermarks or other visible typography in
reference images or keyframes.

The application owns key ordering and validation before a job reaches Salad. The worker also runs the
official Ideogram caption verifier with `raise_on_caption_issues=True` during inference.

## Phase 4 references

`VisualReferenceBot` still uses OpenAI Luna to decide the stable visual identity of each continuity
entity. It now wraps that identity in an Ideogram JSON caption instead of a provider-neutral prose
prompt. `run_phase4_assets.py` submits those captions through the shared Ideogram queue and writes the
returned PNGs to the existing `ReferenceAsset` locations.

Reference generation remains a fan-out workload. The local orchestrator launches the requests
concurrently and Salad distributes queued jobs across up to four replicas.

## Phase 6 keyframes and continuity

The open-weight Ideogram pipeline is text-to-image and does not expose the multi-reference image
conditioning previously used by the OpenAI image provider. Phase 6 therefore no longer reads or sends
binary `ReferenceAsset` files when generating keyframes.

Continuity is carried explicitly in the structured caption. `StoryboardFrameBot` receives:

- the shot action and timing;
- only the canonical `VisualReference` captions for entities present in that shot;
- the previous storyboard caption within the same scene;
- visual style and aspect ratio.

Luna resolves those constraints into a fresh Ideogram caption for the shot. The resulting keyframe job
has zero object inputs. Phase 4 PNGs remain useful persisted project evidence and review artifacts, but
the workflow no longer claims they were consumed by the open-weight model.

## Determinism and replay

Application IDs are deterministic over:

- task purpose (`reference` or `keyframe`);
- structured caption SHA-256;
- model/profile;
- width and height.

The generation seed is derived deterministically from that application job ID. Repeating the same job
therefore reaches the normal Postgres/R2 replay path instead of inventing a new generation identity.

## Container and model bootstrap

The container pins the official `ideogram-oss/ideogram4` runtime source to commit
`990fe1c4e950bb9e9dc90e01c0ad98ba434f83c2`. Model weights come from the gated
`ideogram-ai/ideogram-4-nf4` Hugging Face repository.

The image starts with `HF_HUB_OFFLINE=1`. During bootstrap, `download_models.sh` temporarily enables
network access for `hf download`, resolves the configured `IDEOGRAM_MODEL_REVISION` to a concrete
snapshot, points the local Hugging Face `main` ref to that snapshot, and writes a readiness marker.
The resident runtime then loads from the local cache in offline mode. This avoids silently resolving a
different model snapshot while a worker is already starting.

Before deployment the Hugging Face account behind `HF_TOKEN` must have accepted the model repository's
gate/license. The Ideogram 4 weights are released under the Ideogram 4 Non-Commercial License, which
matches this project's stated noncommercial use; deployment should not treat that as permission for a
future commercial use without re-evaluating the license.

## Salad profile

The initial deployment baseline is:

```text
GPU: RTX 4090 24 GB
min replicas: 0
max replicas: 4
desired queue length: 1
max upscale: 2 replicas/minute
sampler: V4_QUALITY_48
quantization: NF4
compute dtype: bfloat16
```

This is an initial production profile, not a permanent hardware contract. The first real Salad smoke
should record cold bootstrap time, model load time, peak VRAM, per-image latency and output quality for
both 1024x1024 references and 1024x1536 keyframes before considering a cheaper GPU class.

The repository change prepares the worker, Docker image, queue contract and declarative Salad service.
It does not build/push the image or start paid Salad replicas automatically.
