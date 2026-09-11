# Dedicated Breeze TTS 2 worker

Phase 5 narration generation uses a model-specific Salad worker running the open-weight Breeze TTS 2
runtime. OpenAI Speech remains only as a compatibility provider; the production Phase 5 script sends
narration jobs to the dedicated Breeze queue.

## Boundary

```text
SourceScript.text
      |
      v
run_phase5_audio.py
      |
      v
ai-video-factory-breeze-tts2-jobs
      |
      v
docker/workers/breeze-tts2
  BreezeTTS2Backend
  fast-all resident runtime
      |
      v
jobs/<job-id>/narration.wav in R2
      |
      v
SaladBreezeSpeechProvider
      |
      v
NarrationAudio + local narration.wav
```

The worker registers only `audio.breeze_tts2.generate`. R2, Postgres leases, replay, output
reconciliation and the Salad HTTP boundary come from the shared `ai_video_factory.inference` core.

## Runtime profile

The worker mirrors the official Breeze TTS 2 fast runtime instead of spawning the CLI for every job.
The runtime repository is cloned at a pinned Git commit and imported from `/opt/breeze-infer`. Model
weights are downloaded separately into `/workspace/models/breeze-tts-2`.

Initial generation profile:

```text
model: BreezeBlue/Breeze-TTS-2
profile: breeze-tts2-fast-all-v1
fast_all: true
cfg_scale: 4.0
seed: 42
sample format: mono PCM16 WAV
runtime source commit: 008f769016b0a24711becd7a4925030bc93f608c
```

`prepare()` loads the model once, builds `FastBreezeStreamingRuntime`, applies the official fast
warmup profile and retains the warmed runtime in GPU memory. Individual jobs reuse that resident
runtime.

## Voice and delivery

Breeze Voice Design accepts a natural-language instruction instead of a provider-specific voice ID.
The existing provider-neutral `voice` field is therefore interpreted as a voice identity description.
The workflow's `instructions` field remains the delivery direction. The worker combines both into one
Breeze instruction and explicitly asks the model to keep the same speaker identity.

The default English documentary voice is configured through `BREEZE_TTS_VOICE` and can be replaced
without changing domain contracts.

## Long narration

The official single-request example uses a bounded streaming generation profile. To prevent long
scripts from being silently truncated, the worker splits narration at sentence boundaries before
inference when a chunk exceeds `BREEZE_MAX_CHUNK_CHARS` (default 1200). Oversized individual sentences
fall back to whitespace-aware splitting.

Every chunk uses the same voice instruction, delivery direction and deterministic seed. Audio is
written sequentially into one 24 kHz mono WAV with a small configurable inter-chunk pause. Downstream
stages still receive exactly one canonical `narration.wav` and one `NarrationAudio` timeline.

## Speech speed

The historical speech-provider contract allows `speed` from 0.25 to 4.0. Breeze's Voice Design API
does not expose an equivalent numeric speed multiplier, so the worker preserves that contract after
synthesis with FFmpeg `atempo`. Factors outside the single-filter 0.5-2.0 range are decomposed into a
chain, preserving pitch while changing final narration duration.

## Deterministic jobs

Application job identity fingerprints:

- exact source text SHA-256;
- model/profile;
- voice description;
- delivery instructions;
- speed;
- CFG scale;
- seed.

A repeated identical narration request therefore replays through the shared Postgres/R2 idempotency
path instead of synthesizing again.

## Salad deployment

The service entry is `breeze_tts2` in `deploy/salad/services.json`:

```text
queue: ai-video-factory-breeze-tts2-jobs
container group: ai-video-factory-breeze-tts2-worker-v2
GPU: RTX 4090 24 GB
priority: high
min replicas: 0
max replicas: 2
```

The production deployment environment is manifest-owned. In particular, the validated Salad group
uses:

```text
INFERENCE_WORKER_MODE=production
BREEZE_DEVICE=cuda:0
SALAD_QUEUE_ENABLED=true
```

Local `.env` values must not override those deployment-managed values during `Prepare`; `.env` remains
the source for required secrets and external credentials.

Breeze documents approximately 14.4 GiB VRAM for the `--fast-all` path and recommends a 24 GB GPU for
that configuration. RTX 4090 is the initial latency-oriented baseline.

### Validated real deployment baseline

The Breeze worker completed its first end-to-end real Salad smoke on 2026-09-11. The validated group
configuration was version 6 with `priority=high`, zero idle replicas and the pinned image:

```text
docker.io/yagobordell/ai-video-factory@sha256:c83278ad2fcb3b6434e7120fd54ed277d8eea723a808a1c828192429f240e382
```

The smoke exercised the real Salad Job Queue transport, scale-from-zero, runtime bootstrap, inference,
R2 artifact flow and local download. The resulting evidence was:

```text
status: succeeded
wall_seconds: 978.352
artifact: breeze-smoke.wav
size_bytes: 238124
sha256: bc9f0cb0ba8675aadb3247f9c93de532de693189faf31e849b9af2ec69cc97ab
duration_seconds: 4.96
sample_rate: 24000
channels: 1
```

After the smoke, the container group was explicitly stopped and verified at `replicas=0`. This closes
the functional deployment baseline for Breeze TTS 2. Peak VRAM, steady-state real-time factor and
cost/latency optimization remain separate performance measurements and were not inferred from this
smoke.

The validation used `BREEZE_MODEL_REVISION=main`. Pinning the exact model checkpoint revision remains
a deployment-hardening follow-up so future image/config changes cannot silently move the model weights.

Prepare the service with the generic validation manager:

```powershell
.\scripts\manage_salad_validation.ps1 `
  -Service breeze_tts2 `
  -Action Prepare `
  -SkipBuild
```

The manager resolves the current Salad GPU-class UUID for `RTX 4090` at deployment time.

## Container build

The container targets Python 3.12 and CUDA 12.8. It pins:

```text
torch 2.9.1
torchaudio 2.9.1
flash-attn 2.8.3
Breeze runtime commit 008f769016b0a24711becd7a4925030bc93f608c
Ada flash-attention target sm89
```

The model checkpoint revision remains configurable through `BREEZE_MODEL_REVISION`. The validated
functional baseline currently uses `main`; production hardening should replace it with the exact
validated model revision.

## License

Breeze's inference source is Apache-2.0. The open-weight model and self-hosted outputs use the
BreezeBlue Research and Non-Commercial License, which matches this project's declared noncommercial
use. A future commercial deployment must revisit the model/provider choice rather than assuming the
open-weight license permits commercial use.
