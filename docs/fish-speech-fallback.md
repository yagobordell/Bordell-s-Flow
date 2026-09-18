# Fish Speech self-hosted fallback for Phase 5

## Status

This document defines the production design for the Phase 5 fallback:

```text
SourceScript.text
      |
      v
Breeze TTS 2 (primary, Salad)
      |
      +-- success --------------------------> narration.wav
      |
      +-- explicitly eligible terminal failure
                  |
                  v
          stop Breeze / replicas=0
          verify Breeze queue has no active work
                  |
                  v
          Fish Speech S2 Pro (fallback, Salad)
                  |
                  v
             narration.wav
```

The implementation is intentionally **not considered production-validated until the real
Salad smoke, authorized-reference routing smoke, replay test and benchmark are recorded**.
The code and validation harnesses may be merged only after those gates pass.

## No Fish cloud dependency

Fish runs entirely inside a dedicated Salad GPU worker. The runtime does not call Fish
Audio Cloud, does not use the Fish cloud SDK and has no `FISH_API_KEY`. The only external
bootstrap traffic is the pinned official GitHub runtime and the pinned official Hugging
Face checkpoint used while building/bootstrapping the worker.

Service:

- manifest key: `fish_speech`
- container group: `ai-video-factory-fish-speech-worker`
- queue: `ai-video-factory-fish-speech-jobs`
- task: `audio.fish_speech.generate`
- autoscaler: `min_replicas=0`, `max_replicas=1`
- initial GPU: `RTX 4090 (24 GB)`

Fish never shares a container or queue with Breeze.

## Model and reproducibility pins

Selected official self-hosted model:

- repository: `fishaudio/s2-pro`
- model revision:
  `1de9996b6be38b745688de084d87a5633f714e4e`
- Fish Speech runtime repository: `fishaudio/fish-speech`
- runtime commit:
  `214da3cd841bda85da2496b96cd3c4d7edb1337e`
- generation profile: `fish-s2-pro-bf16-v1`
- chunking profile: `sentence-utf8-v1`
- Python: 3.12
- PyTorch: 2.8.0 from the official Fish locked environment
- CUDA wheel family: cu128
- precision: official default BF16 path; no third-party quantization

The official S2 documentation recommends at least 24 GB of VRAM for inference, therefore
RTX 4090 is the first measured target rather than an assumed final answer. The current
official checkpoint tree is approximately 11 GB and includes two sharded model weights
plus `codec.pth`.

Official sources consulted:

- https://github.com/fishaudio/fish-speech
- https://github.com/fishaudio/fish-speech/blob/main/docs/en/inference.md
- https://github.com/fishaudio/fish-speech/blob/main/docs/en/server.md
- https://huggingface.co/fishaudio/s2-pro

The public S2.1 Pro product was evaluated, but no newer official open/self-hostable S2.1
checkpoint superseding `fishaudio/s2-pro` was available in the official self-hosted
model path at implementation time.

## Runtime integration

The worker uses Fish's official Python runtime directly. It instantiates the official
`tools.server.model_manager.ModelManager`, which in turn owns the resident semantic
model queue, decoder and `TTSInferenceEngine`. The model is constructed once during
`prepare()`, warmed once and reused for all jobs. No per-job CLI process and no remote
HTTP API are used.

The shared inference core still owns:

- `/health`, which can answer before model download/load finishes;
- background preparation and `/ready`;
- Salad Queue transport;
- Postgres lease/reconciliation;
- R2 inputs/outputs;
- deterministic replay.

`/ready` is false until the exact bootstrap marker exists, required checkpoint files are
present, the official runtime is resident and CUDA is available.

## Bootstrap

`docker/workers/fish-speech/download_models.sh` downloads only the required S2 Pro files
at the exact model revision into a staging directory. It uses the shared
`download_watchdog` with hard timeout, stall timeout, throughput monitoring and Salad
reallocation on anomalously slow downloads. It validates required files, logs total
checkpoint bytes and atomically moves a `.ready` marker into place only after success.

Because the Hugging Face model is gated for non-commercial acknowledgement, deployment
requires `HF_TOKEN` with access to the official checkpoint. This is unrelated to Fish
Audio Cloud and no `FISH_API_KEY` exists anywhere in the service.

The marker binds both immutable pins:

```text
fishaudio/s2-pro@1de9996b6be38b745688de084d87a5633f714e4e|runtime@214da3cd841bda85da2496b96cd3c4d7edb1337e
```

## Voice reference strategy

Breeze's `voice` remains a natural-language delivery description. Fish's voice is a
separate reference identity and is never inferred from that string.

Production Fish fallback requires these settings:

```text
FISH_SPEECH_REFERENCE_PROFILE=project-narrator-v1
FISH_SPEECH_REFERENCE_AUDIO_KEY=voices/project-narrator-v1.wav
FISH_SPEECH_REFERENCE_AUDIO_SHA256=<64 lowercase hex chars>
FISH_SPEECH_REFERENCE_TRANSCRIPT=<exact words spoken in the reference>
```

The WAV is a project-owned or explicitly authorized asset stored in R2, not committed into
the repository or baked into the Docker image. The transcript must match the audio exactly.
Fish's official material describes short reference clips: the S2 overview says typically
10-30 seconds, while the current WebUI guidance says 5-10 seconds is sufficient. For this
project use one clean 10-20 second mono WAV with little leading/trailing silence and an
exact transcript unless later real validation demonstrates a better project-owned clip.

The shared worker downloads the R2 input and validates its SHA-256 before inference. Fish
then uses its official reference loader with memory caching enabled; repeated chunks reuse
the encoded reference instead of re-encoding a different voice identity.

Unconditioned/random timbre is allowed only behind explicit smoke flags. It is not a valid
production fallback configuration.

## Delivery instructions and speed

The current self-hosted `ServeTTSRequest` exposes sampling/reference controls but does not
provide a provider-neutral `instructions` field equivalent to Breeze's prose delivery
description. S2 supports inline control tokens/tags, but arbitrary natural-language
instructions must not be rewritten into undocumented tags.

Therefore the first production profile:

- never speaks the `instructions` string;
- keeps `instructions` in deterministic identity/provenance;
- sends only the actual narration text to Fish;
- uses the authorized reference for voice identity/style consistency.

Speed is normalized after Fish synthesis with FFmpeg `atempo`, including factor chaining
for the full neutral contract `0.25 <= speed <= 4.0`.

## Canonical audio and long text

Every Fish result is normalized to:

- WAV;
- PCM16;
- mono;
- 24 kHz.

Phase 5 and downstream Whisper therefore do not branch on provider.

Long narration is split deterministically at UTF-8 byte budgets, preferring sentence
boundaries and then whitespace. The splitter asserts that concatenating chunks reconstructs
the exact input text. Every chunk uses the same model, reference, instruction fingerprint,
generation profile and deterministic seed family. Chunk `N` uses `base_seed + N` to
make the entire request reproducible without promising bitwise determinism across CUDA or
library changes. A fixed inter-chunk pause is part of the generation profile.

## Deterministic identity and replay

Fish job IDs use a separate `fish-speech-<hash>` namespace and include:

- provider;
- model repository and exact revision;
- runtime commit;
- generation profile;
- chunking profile;
- narration text hash;
- reference profile/audio SHA-256/transcript SHA-256;
- instructions SHA-256;
- speed;
- seed;
- canonical output format.

A completed R2 object with matching job/request metadata is returned by
`InferenceJobExecutor` before queue submission. That means a replay can succeed while the
Fish group remains stopped at `replicas=0`.

Provider provenance contains the Fish model/revision/profile/job/request/reference/output
hashes, duration, sample format and `replayed`. Controlled fallback additionally records:

```text
provider=fish_speech
fallback_from=breeze_tts2
fallback_reason=<exact classification>
```

## Conservative fallback policy

Fish is allowed only after an explicitly classified terminal Breeze failure:

- bounded pending/running queue timeout;
- terminal queue transport failure;
- terminal Breeze worker inference rejection;
- invalid/corrupt Breeze WAV output;
- controlled Breeze prewarm/bootstrap/ready failure after R2 and Salad control-plane
  preflights have already succeeded.

The fallback does **not** catch arbitrary exceptions. Source validation, invalid
parameters, R2 errors, credentials, Postgres/shared infrastructure failures and application
bugs are not converted into Fish fallback.

`BreezeThenFishSpeechProvider` implements the provider-level policy. Production Salad
lifecycle remains in PowerShell: `run_phase5_audio_controlled.ps1` executes one provider
attempt at a time, stops and verifies Breeze before Fish prewarm, and cleans Fish after
completion.

## Commands

Prepare/deploy the Fish worker:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
    .\scripts\prepare_salad_worker_manifest.ps1 \
    -Service fish_speech \
    -NonInteractive
```

Real protected Fish smoke, fallback-routing smoke and replay:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
    .\scripts\run_fish_speech_smoke_controlled.ps1 \
    -NonInteractive
```

A technical unconditioned runtime smoke is possible before an authorized reference exists:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
    .\scripts\run_fish_speech_smoke_controlled.ps1 \
    -AllowUnconditionedFish \
    -NonInteractive
```

That technical smoke does **not** close the production voice-fallback validation gate.

Normal Phase 5:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
    .\scripts\run_phase5_audio_controlled.ps1 \
    -SourceFile .\data\output\phase2\source_script.json \
    -OutputDir .\data\output\phase5 \
    -Metadata .\data\output\phase5\narration.json \
    -NonInteractive
```

## Real benchmark gate

Do not fill this section from estimates. After `Prepare` pins the deployed image by
digest and the protected smoke succeeds, record the actual values here from Salad and the
generated reports:

| Measurement | Validated value |
| --- | --- |
| Docker digest | `docker.io/yagobordell/ai-video-factory@sha256:ef3ea89dfb09d9d21cb9de768713d60258ac736e494259bca5df6ae46a8f6bb4` |
| Salad GPU | RTX 4090 (24 GB) - PENDING REAL SMOKE |
| checkpoint bytes | PENDING REAL BOOTSTRAP |
| node assignment | PENDING REAL SMOKE |
| image pull/start | PENDING REAL SMOKE |
| bootstrap/download | PENDING REAL SMOKE |
| model load | PENDING REAL SMOKE |
| time-to-ready | PENDING REAL SMOKE |
| resident VRAM | PENDING REAL SMOKE |
| peak VRAM | PENDING REAL SMOKE |
| inference seconds | PENDING REAL SMOKE |
| audio seconds | PENDING REAL SMOKE |
| real-time factor | PENDING REAL SMOKE |
| R2 roundtrip | PENDING REAL SMOKE |
| output bytes | PENDING REAL SMOKE |
| sample format | PCM16 mono 24 kHz - MUST BE RECONFIRMED |
| replay | PENDING REAL REPLAY |
| final replicas/queue | PENDING REAL CLEANUP |

The RTX 4090 decision is provisional until those measurements show stable headroom.

## License

Both the current Fish Speech source and the selected S2 Pro model publish the
**Fish Audio Research License**. The official model card states that research and
non-commercial use is permitted free of charge and commercial use requires a separate
license from Fish Audio. The license also includes attribution/distribution obligations and
other restrictions.

The project's current non-commercial use does not remove the need to comply with all
license terms. Before any commercial deployment, obtain/review the required commercial
license in writing; do not assume the research license extends to paid products, customer
work or internal commercial operations.

This section is operational documentation, not legal advice.
