# LTX 2.5 audio-to-video avatar mode

## Boundary

The A2V capability animates an already-created reference image with an already-cut speech
segment. It deliberately does not know about STT, timeline planning, B-roll selection, or
audio cutting.

```text
future STT/orchestrator
    -> decides the avatar interval
    -> cuts the full narration to one audio segment
    -> supplies the run avatar image
              |
              v
video.ltx25.audio_to_video
    image + audio + prompt/config
              |
              v
existing ltx25 Salad worker
              |
              v
official A2VidPipelineTwoStage
              |
              v
MP4 with conditioned original speech + metadata.json
```

The future orchestration layer therefore only needs to decide that an interval is an avatar
interval and submit the image plus the corresponding audio segment. The LTX provider never
parses STT and never decides where to cut audio.

## Official implementation

The worker remains pinned to the validated LTX source commit:

```text
Lightricks/LTX-2
a95ab856bf29407b6b066ede0abe1846050db56c
```

That revision contains `ltx_pipelines.a2vid_two_stage.A2VidPipelineTwoStage`. The project
uses that implementation directly rather than recreating its denoising or audio-conditioning
logic.

A2V uses the official LTX 2.5 split assets:

```text
diffusion_models/ltx-2.5-22b-dev-transformer-bf16.safetensors
text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
vae/ltx-2.5-video-vae-bf16.safetensors
vae/ltx-2.5-audio-vae-bf16.safetensors
latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors
```

The existing I2V mode continues to use its distilled transformer. Both transformer files are
kept because the official guided A2V two-stage pipeline and the existing distilled I2V
pipeline require different checkpoints.

## One worker, two modes

There is still one `ltx25` Salad service and one queue:

```text
service:  ltx25
group:    ai-video-factory-ltx25-worker-v2
queue:    ai-video-factory-ltx25-jobs-v2

tasks:
- video.ltx25.generate
- video.ltx25.audio_to_video
```

The process has a shared `LTXPipelineModeController`. I2V is warmed during worker startup
exactly as before. The A2V runner validates all of its assets during readiness but does not
also materialize a second 22B pipeline. On a mode switch the active pipeline is released,
Python/CUDA caches are trimmed, and the requested pipeline is loaded. Consecutive jobs in the
same mode reuse the resident pipeline, which is the production path expected when one run
contains several avatar segments.

This avoids deliberately keeping both 22B transformers resident at the same time while
preserving a single queue, job model, R2 transport, Postgres lease model, worker process, and
deployment.

## Salad profile

The A2V-capable worker retains the project's LTX GPU and priority:

```text
GPU:       RTX 5090 (32 GB)
priority:  high
CPU:       8
RAM:       61440 MiB
storage:   171798691840 bytes (160 GiB)
autoscale: min=0, max=4
```

The increased RAM/storage is intentional. The service now needs the existing distilled
transformer plus the official dev transformer and distilled LoRA. The inference policy is:

```text
quantization: FP8_CAST
offload:      CPU
DiffVAE:      CHUNKED_EAGER / eager SDPA compatibility route
```

The eager-SDPA compatibility route is retained from the existing RTX 5090 I2V validation.

## Request contract

The public task is deliberately named by capability, not by bot:

```text
video.ltx25.audio_to_video
```

Inputs are exactly:

```text
image
audio
```

A representative request is:

```python
job_id = ltx_a2v_application_job_id(
    segment_id="003",
    prompt=prompt,
    image_sha256=image_sha256,
    audio_sha256=audio_sha256,
    seed=42,
    width=1280,
    height=720,
    fps=24,
)

request = InferenceJobRequest(
    job_id=job_id,
    task="video.ltx25.audio_to_video",
    inputs=[
        ObjectInput(name="image", key=image_key, sha256=image_sha256),
        ObjectInput(name="audio", key=audio_key, sha256=audio_sha256),
    ],
    output=ObjectOutput(
        key=f"jobs/{job_id}/avatar_segment.mp4",
        content_type="video/mp4",
    ),
    sidecar_outputs={
        "metadata": ObjectOutput(
            key=f"jobs/{job_id}/metadata.json",
            content_type="application/json",
        )
    },
    max_attempts=1,
    parameters={
        "generation_profile": LTX_A2V_GENERATION_PROFILE,
        "prompt": prompt,
        "seed": 42,
        "width": 1280,
        "height": 720,
        "fps": 24,
    },
)
```

There is intentionally no `num_frames` field in the A2V parameter model.

## Single-shot generation policy

Avatar generation is deliberately one-shot. Every
`video.ltx25.audio_to_video` request must set:

```text
max_attempts=1
```

The shared worker enforces this before task execution. If Salad delivers the same failed or
expired application job again, the later delivery is rejected without calling LTX again.
A completed deterministic R2 artifact may still be replayed as the same result; replay never
means regeneration.

There is no automatic regeneration based on visual quality, identity stability, lip-sync,
audio quality, facial motion, or any subjective score. The first generated MP4 is the MP4
that is kept. A future orchestrator must preserve the same rule and must not add a
quality-triggered retry loop.

## Temporal contract

Audio is the source of truth. The worker probes the supplied segment with `ffprobe` and
passes `num_frames=None` to the official A2Vid pipeline. LTX decodes the audio and derives a
frame count on its temporal grid.

At 24 fps the causal VAE grid can differ from arbitrary speech duration by less than one
8-frame interval. The metadata makes that difference explicit:

```json
{
  "input_audio_duration_seconds": 6.8,
  "effective_audio_duration_seconds": 6.7,
  "output_video_duration_seconds": 6.7,
  "duration_delta_seconds": -0.1,
  "fps": 24,
  "num_frames": 161,
  "width": 1280,
  "height": 720,
  "seed": 42,
  "generation_mode": "audio_to_video"
}
```

The official pipeline returns the original decoded conditioning waveform, trimmed only to the
snapped video duration, and that waveform is passed to the official `encode_video` call. The
worker does not synthesize or substitute speech.

The upstream auto-duration helper clamps raw frame requests to 1024 frames and the causal grid
snaps down to the largest valid `8k+1` count. The worker therefore rejects input longer than
the corresponding 1017-frame interval instead of silently truncating it. At 24 fps that hard
maximum is 42.375 seconds.

The production recommended maximum is currently 12 seconds. It is a conservative segment
boundary intended for stable 720p operation and is recorded in metadata. Longer clips up to the
technical maximum are contract-valid, but the future STT/orchestrator should prefer shorter
speech intervals where a natural split exists.

## Geometry and FPS

A2V follows the same output contract as the current main video path:

```text
1280x720
16:9
24 fps
```

LTX's two-stage implementation needs dimensions on its spatial grid, so the canonical 720p
profile is internally conditioned/generated at 1280x768 and center-cropped back to 1280x720.
The avatar image is first aspect-preserving cropped/fitted to the target frame; faces are never
stretched by a non-proportional resize.

The A2V worker does not run Real-ESRGAN. Its MP4 remains compatible with the existing
postprocessing/upscale path.

## Prompt preset

If the caller does not override the prompt, A2V uses one talking-head preset that asks for:

- a medium close-up, camera-facing speaker;
- speech synchronized to the provided audio;
- stable identity, hair, skin tone, clothing, background, and framing;
- natural blinking and restrained facial/head motion;
- a static camera and continuous shot;
- no cuts, scene changes, exaggerated gestures, identity drift, or facial deformation.

This avoids inheriting aggressive cinematic motion language from the general I2V prompts.

## Outputs and diagnostics

Every A2V request declares two deterministic objects under the job prefix:

```text
jobs/<job_id>/avatar_segment.mp4
jobs/<job_id>/metadata.json
```

The shared inference worker gained optional named sidecar outputs. Existing requests that omit
that field keep their previous fingerprint and behavior.

The A2V metadata records:

```text
generation mode/profile
input codec/sample rate/channels
input/effective/output durations
duration delta
fps / num_frames / width / height / seed
model load time
inference time
encode+audio mux time
generation elapsed time
real-time factor
peak CUDA allocation when available
whether the resident A2V pipeline was reused
recommended maximum duration
```

Transport failures still use the shared inference error model. A2V validation provides
specific errors for missing/invalid image, image decode failure, missing/invalid audio, audio
decode failure, unsupported duration, missing model assets, CUDA/runtime failure, inference
failure, encode/mux failure, and sidecar/output upload failures instead of collapsing these
inside the task itself.

## Real Salad smoke

The controlled smoke entrypoint is:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
    .\scripts\run_ltx25_a2v_smoke_controlled.ps1 \
    -NonInteractive
```

By default the smoke uses the real local avatar fixtures in:

```text
data/input/avatar/
```

That directory must contain exactly one supported image (`.png`, `.jpg`, `.jpeg`, or
`.webp`) and exactly one supported audio file (`.wav`, `.mp3`, `.m4a`, `.aac`,
`.flac`, or `.ogg`). These files remain local because `data/input/*` is gitignored.
The wrapper resolves and validates both files before allocating a Salad GPU, so an absent or
ambiguous fixture set fails without incurring GPU cost.

Explicit paths are still supported when a different test pair is needed:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
    .\scripts\run_ltx25_a2v_smoke_controlled.ps1 \
    -AvatarImage .\path\avatar.png \
    -Audio .\path\speech.wav \
    -Prompt "A stable talking head speaking naturally to camera." \
    -NonInteractive
```

The script:

1. prepares the current `ltx25` image/deployment unless `-SkipPrepare` is supplied;
2. boots exactly one cost-guarded RTX 5090 replica;
3. uploads image and speech to R2;
4. submits a real `video.ltx25.audio_to_video` job;
5. waits for terminal status;
6. downloads MP4 and metadata;
7. verifies SHA-256 values;
8. checks one video stream and one audio stream;
9. checks 1280x720 and 24 fps;
10. decodes the full video stream with ffmpeg as a technical integrity check only;
11. prints benchmark fields and artifact paths;
12. stops the LTX service in `finally`.

These checks do not grade visual quality or lip-sync and never trigger regeneration. There is
no manual acceptance step: the generated artifact is retained as-is.

## Benchmark evidence

Real benchmark evidence belongs here after each validated deployment. The smoke emits all of
the following directly from `metadata.json`:

```text
audio duration
resolution / fps / num frames
FP8 + CPU-offload profile
model load time
inference time
encode/mux time
total generation time
real-time factor
peak VRAM
output hash and size
```

Do not infer benchmark numbers from local/unit tests. Only values from a real Salad RTX 5090
run should be recorded as production evidence.

## Future orchestrator

No STT/avatar selection logic is implemented in this change. The future flow remains:

```text
full narration
    -> STT
    -> choose avatar interval
    -> cut exact audio segment
    -> submit image + segment audio + prompt to LTX A2V
    -> place returned segment on the timeline
```

That future work should not require another LTX worker or inference-contract redesign.
