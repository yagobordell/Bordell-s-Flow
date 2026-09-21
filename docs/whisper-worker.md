# Dedicated Whisper worker

Phase 5 word alignment no longer depends on OpenAI transcription. The canonical backend is a
model-specific Salad worker using `openai/whisper-large-v3-turbo` through Hugging Face Transformers.
The domain contract is unchanged: Phase 5 still receives provider-neutral word timestamps and writes
`NarrationWord[]`.

## Boundary

```text
local orchestrator
  run_phase5_alignment.py
       |
       | upload narration.wav to R2
       v
ai-video-factory-whisper-jobs
       |
       v
docker/workers/whisper
  TransformersWhisperBackend
       |
       | return_timestamps="word"
       v
jobs/<job-id>/words.json in R2
       |
       v
SaladWhisperTranscriptionProvider
       |
       v
TranscribedWord[] -> NarrationWord[]
```

The worker registers only `audio.whisper.transcribe`. R2 and Postgres behavior comes from the shared
`ai_video_factory.inference` core; the Whisper package contains only model settings, the Transformers
adapter and its task runner.

## Model profile

The validated profile is:

```text
model: openai/whisper-large-v3-turbo
generation profile: whisper-large-v3-turbo-fp16-no-prompt-greedy-v2
dtype: float16
device: cuda:0
task: transcribe
timestamps: word
language default in Phase 5: en
```

The canonical source script is deliberately not passed into Whisper decoder `prompt_ids`.
For long-form narration the worker transcribes from audio plus the explicit language hint, with
`condition_on_prev_tokens=False` and greedy `temperature=0.0`. The known source script remains
the business truth used by Phase 5 to validate transcript quality and by downstream timing/caption
logic; it is not decoder context.

Word timestamps produced by the Transformers Whisper pipeline are approximate model timestamps; the
existing Phase 5 validation remains responsible for ordering and narration-duration invariants.

## Queue and artifacts

Whisper has a dedicated queue:

```text
SALAD_WHISPER_QUEUE_NAME=ai-video-factory-whisper-jobs
```

Audio is addressed by SHA-256 under:

```text
phase5/whisper/inputs/<audio-sha256>.wav
```

The deterministic application job ID fingerprints the audio SHA, language, model and generation
profile. Results are persisted under:

```text
jobs/<whisper-job-id>/words.json
```

Salad transports only the inference envelope and the small `InferenceJobResponse`; narration audio
and timestamp JSON move through R2.

## Local client

`InferenceJobExecutor` centralizes submit, polling, response-fingerprint validation and output SHA
verification. This is intentionally reusable by the later Breeze TTS 2 and Ideogram workers.

Required local settings are:

```text
SALAD_API_KEY
SALAD_ORGANIZATION
SALAD_PROJECT
SALAD_WHISPER_QUEUE_NAME
R2_ENDPOINT_URL
R2_BUCKET
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
```

Run the real Phase 5 alignment with:

```powershell
python scripts/run_phase5_alignment.py
```

The English pipeline defaults to `--language en`; another supported Whisper language can be supplied
explicitly.

## Container

The image lives at `docker/workers/whisper`. It copies only package sources, installs the shared GPU
worker dependencies plus pinned Transformers, and downloads the model at container bootstrap.

Model bootstrap settings:

```text
WHISPER_MODEL_ROOT=/workspace/models/whisper-large-v3-turbo
WHISPER_MODEL_REPOSITORY=openai/whisper-large-v3-turbo
WHISPER_MODEL_REVISION=main
WHISPER_DEVICE=cuda:0
WHISPER_DTYPE=float16
```

The downloader writes the Hugging Face snapshot into a staging directory, validates that both
`config.json` and non-empty safetensors weights are present, and only then promotes the completed
snapshot into `WHISPER_MODEL_ROOT`. This prevents the background runtime from observing and trying to
load a partially downloaded model.

`WHISPER_MODEL_REVISION` remains configurable so a validated Hugging Face revision can be pinned
without changing the orchestration layer. The production deployment currently uses `main`; pinning an
exact revision is a follow-up hardening task.

## Salad deployment

The declarative service entry is `whisper` in `deploy/salad/services.json`. It uses a separate group
and queue from LTX because one model image must never consume another model's jobs.

The validated hardware profile is `RTX 3090` with 24 GB VRAM at Salad priority `medium`. The real
queue-backed smoke on 2026-09-12 completed successfully with ten ordered word timestamps and no CUDA
OOM. The first cold-start smoke took `1046.931` seconds including scale-from-zero and model bootstrap;
this is a correctness baseline rather than a steady-state inference benchmark.

The no-prompt profile is deployed from the versioned `whisper-large-v3-turbo-v4` image tag. The
immutable digest is resolved and recorded by `Prepare` before validation.

Full validation evidence and the deployment issues found along the way are recorded in
`docs/whisper-salad-validation-2026-09-12.md`.

GPU classes are declared by human-readable name in `deploy/salad/services.json`. During `Prepare`,
`manage_salad_worker.ps1` calls Salad's organization GPU-class endpoint and resolves the current UUID.
This avoids baking provider-specific class IDs into source control.

The generic manager remains:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/manage_salad_worker.ps1 `
  -Service whisper `
  -Action Prepare
```

On a first deployment the validation manager can create the dedicated queue and container group when
they are absent. Queue-repair preflight tolerates a missing queue, then post-`Prepare` validation
checks the resulting group and autoscaler. `Prepare` must still finish with the group stopped at zero
replicas before any `Start` or smoke is attempted.

## Compatibility

`OpenAITranscriptionProvider` remains in the provider package for compatibility and tests, but
`run_phase5_alignment.py` no longer instantiates it and no longer requires `OPENAI_API_KEY` for word
alignment. OpenAI remains the semantic reasoning provider elsewhere in the pipeline.
