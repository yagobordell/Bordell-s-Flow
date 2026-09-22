# LTX 2.5 audio-to-video avatar mode

## Scope

The `ltx25` Salad service supports two independent generation tasks:

- `video.ltx25.generate`: the existing storyboard keyframe Image-to-Video path.
- `video.ltx25.audio_to_video`: image + already-cut speech audio -> synchronized avatar video.

The A2V task intentionally has no STT or timeline responsibility. Future orchestration decides
which interval uses the avatar, cuts the full narration to that interval, and passes the resulting
audio segment to this provider.

```text
STT / future orchestrator
  -> avatar_image + already-cut audio_segment + prompt/config
  -> SaladLTX25A2VProvider
  -> existing inference queue + R2 transport
  -> ltx25 Salad worker
  -> official A2VidPipelineTwoStage
  -> video.mp4 + metadata.json
```

## Upstream implementation

The worker is pinned to Lightricks/LTX-2 commit
`a95ab856bf29407b6b066ede0abe1846050db56c`, which is also the upstream revision already
used by the existing I2V worker. A2V calls the official
`ltx_pipelines.a2vid_two_stage.A2VidPipelineTwoStage`; it does not reimplement the
audio-conditioning algorithm and does not call an external LTX, Comfy, Fal or Replicate API.

A2V uses the split LTX 2.5 pack:

- `diffusion_models/ltx-2.5-22b-dev-transformer-bf16.safetensors`
- `text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors`
- `vae/ltx-2.5-video-vae-bf16.safetensors`
- `vae/ltx-2.5-audio-vae-bf16.safetensors`
- `latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors`
- `loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors`

The existing I2V distilled transformer remains present and unchanged. Shared text encoder, video
VAE, audio VAE, upsampler, model root, container image and cache are reused.

## Runtime and Salad configuration

The A2V task runs in the existing `ltx25` container group and queue. The deployment remains:

- GPU: RTX 5090 (32 GB)
- Salad priority: high
- CPU: 8
- RAM: 40960 MB
- shared memory: 8192 MB
- storage: 128 GiB
- queue: `ai-video-factory-ltx25-jobs-v2`
- model root: `/workspace/models/ltx-2.5`

I2V is prepared eagerly exactly as before. The A2V pipeline validates its assets at startup and
lazy-loads its model graph on the first A2V request, then retains that pipeline for later avatar
segments in the same warm worker. This avoids making every ordinary I2V startup pay for a second
22B pipeline while still allowing multiple avatar segments to reuse one warm container/model
cache.

The selected A2V memory policy is `fp8-cast` on the dev transformer with `CPU` offload. This
matches the proven memory strategy already used by I2V. The existing eager-SDPA DiffVAE
compatibility route for RTX 5090 is also preserved.

A real Salad A2V benchmark is required before claiming a peak-VRAM figure or stable duration
ceiling. No arbitrary 10-second product split is encoded in the worker.

## Geometry and timing

The public production contract remains 1280x720 at 24 fps. LTX internally pads the canonical
720-pixel height to its 64-pixel two-stage grid and center-crops decoded video back to 1280x720.
Avatar images with a different aspect ratio are fitted/cropped proportionally; they are never
stretched non-uniformly.

A2V does not accept or require `num_frames` in its public parameters. It invokes the official
pipeline with `num_frames=None`, so LTX decodes the supplied audio and derives a valid temporal
frame count from the effective audio duration. The generated metadata records:

- input audio duration
- effective snapped duration
- output MP4 duration
- duration delta
- fps and frame count
- width/height/seed
- source audio codec/sample-rate/channels
- image and audio SHA-256
- quantization/offload mode
- model-load, inference and encode timings
- currently measured duration-limit fields, which remain null until a real benchmark establishes
  defensible limits

The official A2V result supplies the original conditioning audio for muxing. The worker passes
`result.audio` directly to the official `encode_video` helper rather than synthesizing or
regenerating speech.

## Job contract

Application code should use
`ai_video_factory.providers.salad_ltx25.SaladLTX25A2VProvider`.

```python
result = provider.generate_avatar_segment(
    segment_id="avatar-shot-003",
    avatar_image=run_avatar_image,
    audio_segment=already_cut_audio_segment,
    prompt=custom_prompt_or_default,
    width=1280,
    height=720,
    fps=24,
    seed=10,
)
```

The underlying request is equivalent to:

```python
InferenceJobRequest(
    task="video.ltx25.audio_to_video",
    inputs=[
        ObjectInput(name="avatar_image", ...),
        ObjectInput(name="audio", ...),
    ],
    output=ObjectOutput(
        key="jobs/<job-id>/video.mp4",
        content_type="video/mp4",
    ),
    sidecar_outputs={
        "metadata": ObjectOutput(
            key="jobs/<job-id>/metadata.json",
            content_type="application/json",
        )
    },
    parameters={
        "generation_profile": "ltx25-a2v-dev-a95ab856-fp8cpu-gridpad-eagersdpa-v1",
        "prompt": "...",
        "seed": 10,
        "width": 1280,
        "height": 720,
        "fps": 24,
    },
)
```

The application job ID includes segment ID, prompt, image hash, audio hash, geometry, fps and seed,
so each segment is independently cacheable/replayable and does not depend on process-global
timeline state.

## Prompt preset

The default prompt is a conservative single-shot talking-head preset: medium close-up, camera
facing, precise synchronization to supplied speech, preserved identity/clothing/framing, natural
blinking and restrained head motion, static camera, no cuts, scene changes, exaggerated gestures
or identity drift. Callers may replace the prompt per segment.

## Outputs and observability

The primary artifact remains an MP4 so existing postprocessing can consume it. A generic,
backward-compatible inference sidecar mechanism adds `metadata.json` without changing legacy
requests or I2V outputs. Legacy request fingerprints are preserved because absent sidecar fields
are excluded from canonical serialization.

A2V metadata provides per-segment timings suitable for benchmark aggregation. The controlled smoke
also prints queue progress, input/output duration, dimensions, fps, frame count, model load,
inference/encode time and output SHA-256.

Peak GPU memory is not yet reported by the upstream adapter. Add that measurement only after it is
validated against the production Salad/NVIDIA runtime rather than reporting an unreliable
allocator value.

## Real Salad smoke

Use a real, redistributable or team-owned avatar PNG and a short speech clip. No private asset is
hardcoded in the repository.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
    .\scripts\run_ltx25_a2v_smoke_controlled.ps1 \
    -AvatarImage .\path\to\avatar.png \
    -Audio .\path\to\speech.wav \
    -NonInteractive
```

Optional `-Prompt` overrides the avatar preset. `-KeepRunning` leaves the warmed LTX deployment
available for additional segments; otherwise the wrapper restores scale-to-zero.

The smoke performs a real R2 + Salad round trip and verifies:

- successful image and audio upload
- A2V queue execution
- downloadable MP4 and metadata sidecar
- exactly one video stream and one audio stream
- requested 1280x720 geometry
- requested fps
- output duration close to source audio within the LTX temporal-grid tolerance
- decodable, non-silent output audio
- hashes and timing metadata

The downloaded review artifacts are written under
`data/output/deployment-validation/ltx25-a2v/`.

Manual QA should check identity stability, mouth motion against speech, static framing, absence of
scene changes and obvious facial deformation.

## Duration benchmark

Do not set a hard or recommended product duration until the real RTX 5090 smoke/benchmark has
measured representative clips. Suggested matrix:

| Speech duration | Purpose |
| --- | --- |
| ~3 s | short phrase |
| ~7 s | normal sentence |
| ~12 s | long sentence |
| ~20 s | stress point |

For each run retain `metadata.json`, `ffprobe.json`, Salad logs and the MP4, then record model
load, inference, encode/mux, total elapsed, output size and peak VRAM if a trustworthy measurement
is available. If the stable technical limit is lower than realistic STT segments, the future
orchestrator—not LTX—owns semantic splitting.

## Failure boundaries

The task validates image presence/decoding, audio stream presence/decoding, duration metadata,
required model assets, requested output geometry and audio presence after encode. Generic
inference infrastructure continues to preserve queue/allocation/startup/lease/storage failures.
The stored failure retains the underlying exception type/message before the transport exposes a
terminal worker rejection.

## Postprocessing

A2V produces the same canonical 1280x720 MP4 geometry as Phase 8 video generation. It deliberately
does not invoke Real-ESRGAN itself. The later project postprocessing/upscale path remains the owner
of any 2x upscale before final assembly.

## Explicit non-scope

This implementation does not:

- run STT
- infer avatar intervals
- cut the full narration
- choose between B-roll and avatar
- assemble avatar/B-roll into the final timeline
- change Whisper, image providers, I2V defaults or the final compositor

Future orchestration only needs to decide “this interval is avatar”, cut the audio from STT
timestamps, and call the A2V provider with that already-cut clip.
