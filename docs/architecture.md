# AI Video Factory — Architecture

This document describes the production architecture after closure of Phase 9 and the optimized end-to-end control plane. Historical Phase 1
agents remain in the repository as experiments/regression fixtures; the production pipeline starts
from a completed source script.

## Architectural principles

The project follows a small set of explicit rules:

1. **Canonical domain contracts stay small.** Persist relationships, IDs and media URIs; keep model
   parameters and provider response objects out of audiovisual domain models unless a downstream
   phase needs them.
2. **Models decide semantic boundaries; Python owns invariants.** LLMs may decide grouping,
   descriptions or prompts, while Python assigns IDs, reconstructs immutable text, checks ordering
   and derives deterministic timelines.
3. **Provider state is explicit.** Stateful model chains use provider-issued response IDs; cloud GPU
   jobs use explicit manifests, transport IDs and transactional state.
4. **Generated media is persisted as artifacts, not hidden state.** Downstream stages consume local
   or object-storage URIs and hashes rather than opaque provider handles.
5. **Cloud transport is not domain identity.** Salad queue job IDs are replaceable transport IDs;
   application job IDs remain deterministic across retries/replays.
6. **The narration timeline is canonical.** GPU frame rounding may produce slightly longer clips, but
   it never rewrites `ShotTiming`.
7. **Expensive stages must be resumable and selectively regenerable.** Completed work is reused only
   after identity/hash validation.
8. **Cache resolution precedes GPU allocation.** Deterministic R2 identity is checked before prewarm
   whenever the request can already be constructed.
9. **Scheduling follows data dependencies, not phase numbering.** Independent CPU/LLM/GPU work may
   overlap, while stateful semantic chains and constrained shared model services remain serialized.

## Production pipeline

```text
SourceScript
   |
   v
NarrativeBlock[]
   |
   v
Beat[]
   |
   v
Scene[]
   |
   v
Shot[]
   |
   +---------------------------+
   |                           |
   v                           v
ContinuityEntity[]       NarrationAudio
   |                           |
   v                           v
VisualReference[]        NarrationWord[]
   |                           |
   v                           v
ReferenceAsset[]          BeatTiming[]
   |                           |
   |                           v
   |                      ShotTiming[]
   |                           |
   +-------------+-------------+
                 |
                 v
          StoryboardFrame[]
                 |
                 v
        StoryboardKeyframe[]
                 |
                 +-------------------+
                 |                   |
                 v                   v
          StoryboardGrid[]      VideoPrompt[]
                                     |
                                     v
                              GPUJobRequest[]
                                     |
                                     v
                           Salad + GPU worker
                                     |
                                     v
                                VideoClip[]
                                     |
                                     v
                         Phase 9 compositor
```

## Canonical domain contracts

```text
SourceScript        = { text }
NarrativeBlock      = { id, text }
Beat                = { id, block_id, action }
Scene               = { id, beat_ids }
ContinuityEntity    = { id, kind, name, description }
BlockContinuity     = { block_id, entity_ids }
Shot                = { id, scene_id, beat_ids, entity_ids, action }
VisualReference     = { entity_id, prompt }
ReferenceAsset      = { entity_id, uri }
NarrationAudio      = { uri, duration_seconds }
NarrationWord       = { id, text, start_seconds, end_seconds }
BeatTiming          = { beat_id, start_word_id, end_word_id, start_seconds, end_seconds }
ShotTiming          = { shot_id, start_seconds, end_seconds }
StoryboardFrame     = { shot_id, prompt }
StoryboardKeyframe  = { shot_id, uri }
StoryboardGrid      = { scene_id, uri }
VideoPrompt         = { shot_id, prompt }
VideoClip           = { shot_id, uri }
```

Operational contracts such as `GPUJobRequest`, `GPUJobResponse`, queue snapshots, manifests, leases,
object metadata and benchmark reports are intentionally separate from these audiovisual contracts.

## End-to-end scheduling and resource lifecycle

The production phase list is represented as an explicit DAG. Phase numbering remains useful for
artifacts and documentation, but it is not treated as a mandatory global serialization barrier.

```text
phase2
  |------------------------------|
  v                              v
continuity                  narration [Breeze]
  |-----------|                  |
  v           v                  v
shots   reference prompts    alignment [Whisper]
  |           |                  |
  |           +-> reference assets [Ideogram]
  |                              v
  +------------------------ beat/shot timing
                                 |
                            storyboard
                              |     |
                              v     v
                       video prompts keyframes [Ideogram]
                              |     |
                              +--+--+
                                 v
                            videos [LTX]
                                 |
                                 v
                         Remotion + FFmpeg
```

The scheduler has two independent bounds: total concurrent stages and concurrent GPU-backed stages.
A resource key adds model-level mutual exclusion. This prevents Phase 4 and Phase 6 from contending
for the current single-replica Ideogram service even though unrelated model services may overlap.

The normal end-to-end mode temporarily retains a successful Phase 4 Ideogram replica until Phase 6
has consumed or replayed its keyframes. That lifetime extension is bounded: Phase 6 explicitly
releases the shared service, and the outer PowerShell `finally` stops all project services and cleans
queues even when an upstream stage fails.

Fish Speech is intentionally not part of predictive prewarm. Breeze remains primary, and Fish can be
allocated only after a classified eligible Breeze failure and after Breeze has been stopped at zero
replicas.

### Cache identity before allocation

The shared inference cache requires matching application job ID, request SHA-256, output content type,
non-zero size and artifact SHA metadata. The request identity includes the model/generation profile
and all provider parameters used by the worker.

Production applies this before GPU lifecycle decisions for:

- Breeze narration;
- Qwen-Image-2.1 references and keyframes with deterministic R2 replay;
- LTX clips.

Phase 8 repeats the cache check inside `run_video_generation()` before queue reconciliation. This is
important for recovery when local manifests or downloaded MP4s are missing but the deterministic R2
artifact is still valid.

Scheduling metadata itself is not an audiovisual input and is therefore excluded from stage artifact
fingerprints. A scheduler upgrade must not force expensive regeneration of otherwise identical
outputs.

## Phase 2 — Narrative planning

Production begins from immutable `SourceScript.text`.

```text
SourceScript
   |
   v
NarrativeBlockBot
   |
   v
NarrativeBlock[]
   |
   +--> BeatExtractorBot (parallel per block)
   |         |
   |         v
   |       Beat[]
   |         |
   +---------+
             |
             v
      ScenePlannerBot
             |
             v
          Scene[]
```

The model chooses narrative and beat boundaries. Python reconstructs exact source text, assigns IDs,
restores global ordering after parallel work and validates coverage.

## Phase 3 — Continuity and shots

`ContinuityBot` runs serially across narrative blocks using a provider-managed state chain. It emits
stable physical entities and block/entity relationships. IDs are assigned by Python.

`ShotPlannerBot` runs serially by scene and consumes canonical continuity IDs. Python validates exact
beat coverage and preserves scene/shot order.

Provider state is never treated as canonical project memory. The handoff between stages is persisted
JSON.

## Phase 4 — Visual references

`VisualReferenceBot` creates one reusable identity/environment description per continuity entity.
Python applies entity-kind templates and sends the prompt to an image provider.

```text
ContinuityEntity[]
       |
       v
VisualReference[]
       |
       v
ReferenceAsset[] + PNG
```

`ReferenceAsset.uri` is the persisted boundary. Raw image-provider responses and binary payloads are
not domain state.

## Phase 5 — Audio and timing

Narration is generated as one continuous audio asset to preserve prosody.

```text
SourceScript.text
       |
       v
NarrationAudio + WAV
       |
       v
TranscriptionProvider
       |
       v
NarrationWord[]
       |
       v
BeatTimingBot -> model-owned end-word boundaries
       |
       v
Python reconstruction
       |
       v
BeatTiming[]
       |
       v
ShotTiming[]
```

The model never invents timestamps. Python maps selected word boundaries to measured timing evidence
and derives shot intervals deterministically. The timeline is continuous and is authoritative for
later composition.

## Phase 6 — Storyboard architecture

### Prompt planning

`StoryboardFrameBot` receives shot action, measured duration, canonical visual references and only
the previous storyboard prompt within the same scene.

The previous generated image is not used as the next image input. Identity continuity comes from
canonical references rather than image-to-image propagation.

### Keyframe generation

Each shot resolves only its own referenced entity assets. Keyframes are generated independently and
concurrently, allowing selective regeneration without contaminating later shots.

### Scene grids

Pillow composes review-only scene contact sheets from canonical keyframes. The operation is local and
deterministic; grids preserve shot order and keyframe aspect ratio without modifying source PNGs.

## Phase 7 — GPU infrastructure

### Benchmark boundary

Phase 7 first established a reproducible LTX benchmark rather than hardcoding a cloud GPU profile.
Python owns repetitions, output validation, hashing and `nvidia-smi` sampling. LTX owns inference.

The validated baseline is an RTX 5090 using `fp8-cast` with CPU offload. The closure matrix measured
121 frames at 768x1280 and recorded approximately 194.93 seconds mean runtime with 24,513 MiB peak
VRAM. This remains a replaceable production baseline, not a permanent domain constraint.

### Worker boundary

```text
Salad input
    |
    v
GPUJobRequest
    |
    v
transactional application-job claim + lease
    |                         |
    v                         v
R2 validated inputs       task runner
                              |
                              v
                     deterministic R2 output
                              |
                              v
                      Postgres success commit
```

`GPUJobRequest.job_id` is application identity. Salad's queue ID is transport identity.

Canonical request JSON produces an immutable request SHA-256. Reusing an application job ID with a
different request is a conflict. A completed Postgres row replays. Matching R2 output can reconcile a
crash between upload and success commit. Foreign object metadata is never overwritten.

Postgres owns atomic claims, attempt counts, leases and heartbeat renewal. Losing the lease prevents
artifact commit.

Phase 7 closed the infrastructure with a real cloud smoke, idempotent replay, digest-pinned image and
real LTX benchmark.

## Phase 8 — Video generation architecture

Phase 8 reuses the Phase 7 worker/storage/state boundary and registers the direct LTX task:

```text
video.ltx25.generate
```

There is no ComfyUI dependency.

### 8.1 Motion prompt boundary

Static composition and motion are separate concerns:

```text
Shot + ShotTiming + visual/storyboard context
                    |
                    v
             VideoPromptBot
                    |
                    v
VideoPrompt = { shot_id, prompt }
```

The motion prompt describes subject/camera motion for an already selected canonical keyframe. It does
not redefine identity, timing or static composition.

### 8.2 Direct LTX adapter

The validated generation profile is:

```text
ltx25-distilled-a95ab856-fp8cpu-v1
```

`LTXVideoParameters` validates profile, prompt, seed, dimensions, fps and frame count. Dimensions must
be divisible by 64 and frame count must satisfy `8k + 1`.

The frame count is derived from canonical shot duration by rounding upward to the next valid LTX
shape:

```text
minimum_frames = ceil(shot_duration * fps)
num_frames     = next value satisfying 8k + 1
```

This may make the generated media a few frames longer than `ShotTiming`. That extra media is not a
new timing contract. Phase 9 must still use `ShotTiming` as the timeline authority and trim/compose
media accordingly.

The production model files are resolved below `LTX_MODEL_ROOT`:

```text
diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
vae/ltx-2.5-video-vae-bf16.safetensors
vae/ltx-2.5-audio-vae-bf16.safetensors
latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
```

The direct backend prepares the pipeline once, keeps it resident and serializes access with an
in-process lock.

### 8.3 Real production worker

The direct adapter was validated through the full cloud boundary:

```text
Storyboard keyframe in R2
        |
        v
Salad queue transport
        |
        v
GPUJobRequest
        |
        v
Postgres claim/lease
        |
        v
direct LTX inference on RTX 5090
        |
        v
silent H.264 MP4 -> R2
        |
        v
GPUJobResponse
```

Exact resubmission of a completed application request returns the persisted result with
`replayed=true`; it does not reinfer.

The same warmed instance successfully processed multiple real jobs sequentially, proving that model
construction can be amortized across a batch.

### 8.4 Deterministic fanout and resume

Before any submission, Python builds every `GPUJobRequest` and computes a run fingerprint from the
ordered set of shot IDs, deterministic application job IDs and request SHA-256 values. The workflow
then checks the deterministic R2 output for every request before reading or creating a Salad
transport. A valid object is promoted directly to a replayed succeeded response.

Application IDs are derived from:

```text
shot_id
generation_profile
motion_prompt
keyframe_sha256
seed
width
height
fps
num_frames
```

and have the stable form:

```text
phase8-shot-<shot_id>-<hash-prefix>
```

The Salad transport ID is deliberately excluded.

The resumable manifest stores, per shot:

```text
shot_id
application_job_id
request_sha256
transport_job_id
transport_status
submission_count
validated GPUJobResponse
```

Resume behavior:

| Manifest state | Action |
| --- | --- |
| unsubmitted | submit |
| pending/running | query existing transport |
| succeeded + validated response | no query and no submit |
| failed/cancelled | explicit next-run resubmission with same application ID |

All unresolved shots are submitted before polling begins. This exposes the whole batch to the queue
and lets one warmed GPU consume the jobs sequentially.

Manifest writes are atomic (`temporary file + os.replace`). A changed generation plan is rejected by
run fingerprint rather than mixed with old state.

### 8.4 fan-in

Once all transports succeed, each `GPUJobResponse` is checked against the planned application ID,
request fingerprint and deterministic R2 output key. MP4s are downloaded to canonical local names.

An existing local clip is reused only when size and SHA-256 match the worker response. The canonical
result is:

```text
VideoClip = { shot_id, uri }
```

The final array is ordered by shot ID and persisted as `video_clips.json`.

### Real 8-shot closure

The cloud closure validated one manifest containing all eight shots. Shots 1, 5 and 8 replayed prior
successful application jobs; shots 2, 3, 4, 6 and 7 performed real inference. All clips passed local
size/SHA-256 checks, H.264 probing, 768x1280 resolution, 24 fps, expected frame count and no-audio
validation.

A second completed invocation produced zero new submissions, zero new transport IDs and an unchanged
manifest SHA-256.

The group was then stopped with zero replicas.

Detailed evidence is recorded in:

- `docs/phase8.4-validation-results.md`
- `docs/phase8-closure.md`

### Operational cloud configuration

The configuration encoded by `scripts/manage_phase8_worker.ps1 -Action Prepare` must match the
settings proven by the real run:

```text
priority: medium
liveness path: /health
liveness period: 30 s
liveness timeout: 10 s
liveness failure threshold: 20
queue autoscaler min: 0
queue autoscaler max: 1
```

The wider liveness tolerance is required because the old `timeout=5 / failure_threshold=3` policy
could kill a healthy process during expensive LTX construction/inference.

`Prepare` also supplies Hugging Face authentication securely from `HF_TOKEN` and pins the published
image by digest before patching Salad.

The normal run path should start/stop the already prepared group; `Prepare` is an intentional image
or configuration upgrade operation, not a per-batch step.

## Phase 9 compositor boundary

Phase 9 should consume persisted audiovisual contracts, not GPU/cloud implementation details.

Minimum useful handoff:

```text
VideoClip[]
ShotTiming[]
NarrationAudio
NarrationWord[]
```

Responsibilities:

```text
VideoClip[] + ShotTiming[]
            +
       NarrationAudio
            +
       NarrationWord[]
            |
            v
       composition timeline
            |
      +-----+-----------------------------+
      |     |             |               |
      v     v             v               v
   trim   transitions   captions   overlays/motion graphics
      \     |             |               /
       +----+-------------+---------------+
                         |
                         v
              audio/video final mux
```

Phase 9 may probe/transcode clips using FFmpeg/ffprobe and render timeline/motion graphics with
Remotion. It must not require Salad queue IDs, application-job fingerprints, Postgres rows or LTX
checkpoint paths.

`ShotTiming` remains the timing authority even when an LTX clip contains extra rounded frames.

## Provider boundaries

### Structured text

`StructuredTextProvider` handles independent Structured Output transforms.
`StatefulStructuredTextProvider` adds provider-managed state and returns an explicit response ID.

### Images

`ImageProvider` handles base generation. `ReferenceAwareImageProvider` adds optional reference-image
conditioning without changing the persisted `ReferenceAsset` boundary.

### Speech

`SpeechProvider.generate_speech()` returns ephemeral generated bytes; the domain persists
`NarrationAudio` and its URI.

### Transcription

`TranscriptionProvider` returns normalized word timing evidence. The domain persists
`NarrationWord[]`, not raw provider response JSON.

### GPU queue

`JobQueueClient` is provider-neutral. The Salad implementation converts queue responses into
`QueueJobSnapshot` objects. Workflow code depends on the protocol rather than Salad's raw API shape.

## Persisted artifacts

```text
data/output/phase2/
  source_script.json
  narrative_blocks.json
  beats.json
  scenes.json

data/output/phase3/
  entities.json
  block_continuity.json
  shots.json

data/output/phase4/
  visual_references.json
  reference_assets.json
  reference_assets/*.png

data/output/phase5/
  narration.wav
  narration.json
  narration_words.json
  beat_timings.json
  shot_timings.json

data/output/phase6/
  storyboard_frames.json
  storyboard_keyframes.json
  storyboard_keyframes/*.png
  storyboard_grids.json
  storyboard_grids/*.png

data/output/phase8/
  video_prompts.json
  video_generation_manifest.json
  video_clips.json
  video_clips/*.mp4
```

The Phase 8 manifest is operational resume state. The Phase 9 handoff is `VideoClip[]` plus the
canonical timing/audio artifacts from Phase 5.

## Current implementation status

Completed production stages:

1. narrative block planning;
2. beat extraction and scene planning;
3. continuity registry and shot planning;
4. visual references and reference assets;
5. narration, word alignment and deterministic timing;
6. storyboard prompts, keyframes and scene grids;
7. reproducible GPU benchmark and idempotent cloud worker infrastructure;
8. motion prompts, direct LTX-2.5 execution, real GPU worker, resumable fanout and verified
   `VideoClip[]` fan-in;
9. frame-exact Remotion/FFmpeg compositor producing validated `FinalVideo`;
10. dependency-aware end-to-end orchestration with cache-before-GPU, bounded parallelism, automatic
    resume and final resource cleanup.

Next implementation stage: **Phase 10 — verification agents**.
