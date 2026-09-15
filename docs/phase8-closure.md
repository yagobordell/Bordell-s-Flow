# Phase 8 — Production video generation closure

Phase 8 is closed after validating the complete direct LTX-2.5 production path on Salad. The original closure included a complete 8-shot run on the earlier shared Phase 8 worker slot; the current dedicated LTX service was revalidated end to end on 2026-09-15.

The canonical boundary is:

```text
StoryboardKeyframe[] + VideoPrompt[] + ShotTiming[]
                         |
                         v
              deterministic GPUJobRequest[]
                         |
                         v
          ai-video-factory-ltx25-jobs
                         |
                         v
        ai-video-factory-ltx25-worker
                         |
                         v
          direct LTX-2.5 Python/PyTorch
                         |
              +----------+----------+
              |                     |
              v                     v
      Supabase/Postgres          Cloudflare R2
       leases + state           MP4 artifacts
              \                     /
               +---------+----------+
                         |
                         v
                    VideoClip[]
```

There is no ComfyUI dependency in the production video-generation path.

## Closure status

| Subphase | Result | Production evidence |
| --- | --- | --- |
| 8.1 motion prompts | closed | 8 canonical `VideoPrompt` objects generated and reviewed |
| 8.2 direct LTX adapter | closed | direct distilled LTX-2.5 backend, validated parameters and frame rounding |
| 8.3 real GPU worker | closed | real RTX 5090 inference on both historical and current dedicated worker deployments |
| 8.4 fanout/resume | closed | complete 8-shot queue fanout, resume, fan-in, SHA verification and zero-submit rerun |
| 8.5 closure/hardening | closed | dedicated worker/queue, protected smoke lifecycle, model-download watchdogs and Phase 9 handoff documented |

## Canonical Phase 8 contracts

Phase 8 keeps the audiovisual contract deliberately small:

```text
VideoPrompt = { shot_id, prompt }
VideoClip   = { shot_id, uri }
```

Provider-specific generation parameters, queue transport IDs, request fingerprints and worker responses are operational state. They remain in `video_generation_manifest.json` rather than being copied into `VideoClip`.

The application job identity is deterministic and independent of Salad transport identity:

```text
phase8-shot-<shot_id>-<plan_hash_prefix>
```

The hash covers the request-defining plan inputs: shot, generation profile, prompt, keyframe hash, seed, dimensions, fps and LTX-valid frame count.

## Validated LTX production profile

```text
task:               video.ltx25.generate
generation_profile: ltx25-distilled-a95ab856-fp8cpu-v1
resolution:         768x1280
fps:                24
quantization:       fp8-cast
offload:            CPU
GPU class:          RTX 5090
```

The worker loads the LTX-2.5 distilled model set directly from Python/PyTorch and keeps the pipeline resident while an instance consumes jobs sequentially.

The checkpoint paths are:

```text
diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
vae/ltx-2.5-video-vae-bf16.safetensors
vae/ltx-2.5-audio-vae-bf16.safetensors
latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
```

Phase 8 outputs are silent MP4 clips. Narration and final muxing belong to Phase 9.

## Current dedicated Salad baseline — 2026-09-15

The current production deployment is defined by the `ltx25` service in `deploy/salad/services.json`:

```text
worker group:          ai-video-factory-ltx25-worker
queue:                 ai-video-factory-ltx25-jobs
validated group version: 5
GPU class id:          851399fb-7329-4195-a042-d6514b28cf33
cpu:                   8
memory:                40960 MiB
shared memory:         8192 MiB
storage:               137438953472 bytes
priority:              medium
queue min replicas:    0
queue max replicas:    4
liveness path:         /health
liveness period:       30 s
liveness timeout:      10 s
liveness failures:     20
```

The immutable image validated on 2026-09-15 was:

```text
docker.io/yagobordell/ai-video-factory@sha256:598d743b82f75e531cf29c521530a5b9d8d606a6d98a4e7b9fd737811c384a02
```

`Prepare` builds and publishes the dedicated image only when an intentional image/config update is required, resolves the mutable tag to an immutable digest, and leaves the group stopped at zero replicas.

The model downloader emits byte progress every 30 seconds and aborts one model transfer after 600 seconds with no observed byte growth. The protected LTX smoke allows a healthy cold bootstrap up to 45 minutes while delaying running/not-ready reallocation until 60 minutes.

## Dedicated-worker Phase 8.3 revalidation — 2026-09-15

The current architecture passed a real one-second cloud smoke through the full path:

```text
local orchestrator
  -> R2 keyframe upload
  -> ai-video-factory-ltx25-jobs
  -> dedicated RTX 5090 worker
  -> Postgres claim/lease
  -> direct LTX-2.5 inference
  -> R2 MP4 output
  -> local artifact download + SHA verification
```

Validated request and result:

```text
application_job_id: phase8-shot-001-730e00d82f95
salad_job_id:       e06633cb-814e-4e77-99c3-a2b5b0f2f9fd
resolution:         768x1280
fps:                24
num_frames:         25
seed:               43
replayed:           false
status:             succeeded
output sha256:      ff82d028b04bb5cf91a7198bf75f2e7cf6e585e1cbf25ca8679b376956dc655a
```

All five required model files completed bootstrap. The worker reached `ready=true`, received the job, temporarily became not-ready while busy, completed the job, and returned to ready. Salad's queue attachment observation was false, but the actual queue transport delivered the job successfully; attachment listing therefore remains observational rather than a hard readiness gate.

The cost-guarded wrapper completed its `finally` cleanup with:

```text
status=stopped
replicas=0
pending=False
```

Detailed evidence: [`ltx25-salad-validation-2026-09-15.md`](ltx25-salad-validation-2026-09-15.md).

## Historical Phase 8.3/8.4 evidence

Before the worker was split into a dedicated service, the Phase 8 closure validated a complete production batch on the earlier shared worker slot. That historical run remains evidence for application-level fanout, hot-worker reuse, replay/resume and media verification; it is not the current deployment configuration.

The canonical 8-shot validation used run fingerprint:

```text
c90751bedd2dc65dac7ca7fba927b706ad514e1f72a91edc913c96465541e361
```

The deterministic application jobs were:

```text
1  phase8-shot-001-7ed73f1ff682
2  phase8-shot-002-8c1e5597aad9
3  phase8-shot-003-1fe7826f603e
4  phase8-shot-004-b745637909c4
5  phase8-shot-005-69262d45109b
6  phase8-shot-006-a1aa782f7543
7  phase8-shot-007-dac16fef77d7
8  phase8-shot-008-a617c67ccd15
```

All eight transports reached `succeeded` with `submission_count=1`. Shots 1, 5 and 8 replayed already completed logical jobs; shots 2, 3, 4, 6 and 7 performed real inference.

The final media verification was:

| Shot | Frames | Duration | Replay | SHA-256 prefix |
| ---: | ---: | ---: | :---: | --- |
| 1 | 89 | 3.708 s | yes | `2f5d01380ecf` |
| 2 | 185 | 7.708 s | no | `da70e13fea89` |
| 3 | 129 | 5.375 s | no | `3175fb0f51f3` |
| 4 | 73 | 3.042 s | no | `25dd8ccf280e` |
| 5 | 233 | 9.708 s | yes | `51ba6580ecfa` |
| 6 | 161 | 6.708 s | no | `6030f589b6be` |
| 7 | 185 | 7.708 s | no | `72314f742619` |
| 8 | 65 | 2.708 s | yes | `4d15fe9c9662` |

Every clip passed R2/local size comparison, SHA-256 verification, one H.264 stream, 768x1280 dimensions, 24 fps, expected LTX-valid frame count and no audio stream.

The successful manifest SHA-256 was:

```text
73bb97a822e7c9e4bfe6e4ac79abf94a19424edab7c52d3675ce47e7a859609f
```

A second complete invocation reused the same manifest and finished with zero new submissions, zero new transport IDs and the same manifest SHA-256. Detailed historical evidence remains in [`phase8.4-validation-results.md`](phase8.4-validation-results.md).

## Production commands

The normal Phase 8 orchestration flow remains:

```powershell
# Queue unresolved canonical shots.
python scripts\run_phase8_videos.py --submit-only

# Enable the prepared LTX service with scale-to-zero.
.\scripts\start_phase8_autoscaled.ps1

# Resume the manifest and fan in completed clips.
python scripts\run_phase8_videos.py

# Stop when the batch is complete.
.\scripts\manage_phase8_worker.ps1 -Action Stop
```

The compatibility wrappers above route to the dedicated LTX service. New operational tooling should prefer the service-aware manager directly.

For a real correctness smoke, use the protected command rather than a manual paid-GPU sequence:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_ltx25_protected_smoke.ps1 `
  -SkipLocalBuild
```

Do not rerun that expensive smoke merely for documentation or local-only control-script changes once the validated deployment digest is unchanged.

## Artifact boundary

A completed Phase 8 run writes:

```text
data/output/phase8/
├── video_prompts.json
├── video_generation_manifest.json
├── video_clips.json
└── video_clips/
    ├── shot_001.mp4
    ├── ...
    └── shot_NNN.mp4
```

`video_generation_manifest.json` is resumable operational state. `video_clips.json` is the canonical handoff to the next media stage.

## Phase 9 handoff

Phase 9 must not depend on Salad transport state, GPU request schemas or LTX internals. Its persisted inputs are:

```text
VideoClip[]
ShotTiming[]
NarrationAudio
NarrationWord[]
```

The compositor may probe/transcode individual clips as needed, but it should treat `VideoClip.uri` and `shot_id` as the Phase 8 contract. Timing remains owned by `ShotTiming`; Phase 8's LTX frame rounding is media-generation detail and must not redefine the canonical narration timeline.

## Closure decision

Phase 8 remains closed because all of the following are true:

1. motion is planned separately from static storyboard composition;
2. LTX-2.5 runs directly from Python/PyTorch with the validated distilled profile;
3. the worker is transactional, leased and idempotent across Postgres/R2;
4. historical production validation proved sequential hot-worker jobs and full 8-shot fanout/resume;
5. completed logical jobs replay instead of reinferring;
6. verified canonical `VideoClip[]` output was produced for the complete storyboard;
7. the current dedicated LTX worker/queue architecture has now passed a fresh real cloud smoke;
8. model bootstrap progress and stall behavior are observable and cost-guarded;
9. the protected smoke guarantees the normal Stop path in `finally`;
10. the validated 2026-09-15 run ended with the GPU group stopped at zero replicas;
11. Phase 9 consumes canonical local media contracts without depending on GPU transport state.

Further LTX work should be optimization or maintenance against this correctness baseline, not a reopening of Phase 8.3.
