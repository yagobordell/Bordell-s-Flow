# Salad startup performance hardening

The GPU workers remain self-hosted on SaladCloud. No hosted model inference API is used by this design.

## Goals

Cold starts are split into independent phases so a slow Salad node can be rejected early instead of consuming the entire job timeout:

1. Salad allocation
2. container image pull
3. model download
4. model/runtime preparation
5. queue dispatch
6. inference

Jobs are submitted only after the selected worker reports `ready=true` in controlled flows. Queue pending timeouts therefore measure queue dispatch rather than GPU allocation and model bootstrap.

## Optimized prewarm

`scripts/start_salad_optimized_prewarm.ps1` temporarily requests exactly one replica while the queue autoscaler remains at `min_replicas=0`. It applies service-specific, bounded watchdogs to Whisper, Breeze TTS 2, Ideogram 4, and LTX 2.5.

The prewarm can reallocate a node when allocation is unusually slow, when the container image pull stops making meaningful progress, or when a started container remains not-ready beyond the service budget. Reallocations are capped. The last candidate receives a longer final window rather than cycling indefinitely.

A successful prewarm requires exactly one started, ready replica and an empty queue. Callers must always restore scale-to-zero and stop the group in a `finally` block.

## Model download quality gate

Ideogram, Breeze and Whisper use `ai_video_factory.workers.download_watchdog` around `hf download`. The watchdog measures real bytes written under the model directory and records a moving MiB/s throughput window.

It aborts on:

- no byte progress for the configured stall interval;
- a hard absolute model-download deadline;
- sustained throughput below the conservative per-service threshold after a grace period.

For slow/stalled model downloads the worker requests self-reallocation through Salad IMDS (`POST http://169.254.169.254/v1/reallocate`). This uses Salad infrastructure only and does not call an external inference service.

The configured initial thresholds are deliberately conservative:

- Whisper: 4 MiB/s minimum after 90 s grace
- Breeze TTS 2: 6 MiB/s minimum after 120 s grace
- Ideogram 4: 8 MiB/s minimum after 180 s grace

These values are deployment tuning knobs in `deploy/salad/services.json`; they can be adjusted from measured production logs without changing Python code.

## Hugging Face transport

Ideogram, Breeze and Whisper explicitly install `hf-xet==1.6.0`. Xet is left enabled so large model files can use the current Hugging Face transport implementation.

`HF_XET_HIGH_PERFORMANCE` is intentionally not enabled globally. The workers currently have 16-32 GiB of RAM, below the environment for which Hugging Face recommends that aggressive mode. Network and metadata timeouts remain bounded through `HF_HUB_DOWNLOAD_TIMEOUT` and `HF_HUB_ETAG_TIMEOUT`.

LTX already uses its pinned Xet-aware download path and keeps its existing worker image. The optimized external prewarm adds the same bounded node-selection behavior without rebuilding LTX.

## Controlled production flows

The following wrappers perform optimized prewarm before queue submission and always Stop + Status in `finally`:

- `run_phase4_assets_controlled.ps1` — Ideogram references
- `run_phase5_audio_controlled.ps1` — Breeze narration
- `run_phase5_alignment_controlled.ps1` — Whisper alignment
- `run_phase6_keyframes_controlled.ps1` — Ideogram keyframes

Once a worker is ready, these flows use short queue-pending deadlines. Cold-start time is never charged against the inference job's queue timer.

## Deployment versions

This hardening changes the worker images for:

- Ideogram: `ideogram4-nf4-quality48-v3`
- Breeze: `breeze-tts2-fast-all-v2`
- Whisper: `whisper-large-v3-turbo-v2`

Each changed worker requires one `Prepare` before its next use. Prepare can be deferred until that service is actually needed. LTX is unchanged and does not require another Prepare for this hardening.

Scale-to-zero remains enabled for every service. Ideogram remains capped at one replica.
