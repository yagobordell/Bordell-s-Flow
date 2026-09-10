# Phase 8 — Production video generation closure

Phase 8 is closed after validating the complete direct LTX-2.5 production path on Salad and
hardening the operational tooling around the cloud configuration that survived the real 8-shot run.

The final boundary is:

```text
StoryboardKeyframe[] + VideoPrompt[] + ShotTiming[]
                         |
                         v
              deterministic GPUJobRequest[]
                         |
                         v
                   Salad Job Queue
                         |
                         v
            one warmed RTX 5090 worker
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
| 8.3 real GPU worker | closed | real Salad RTX 5090 inference, worker replay and sequential hot-worker execution |
| 8.4 fanout/resume | closed | complete 8-shot queue fanout, resume, fan-in, SHA verification and zero-submit rerun |
| 8.5 closure/hardening | closed | validated deployment settings persisted, canonical job-ID helper reused, operator progress output and Phase 9 handoff documented |

## Canonical Phase 8 domain contracts

Phase 8 keeps the audiovisual contract deliberately small:

```text
VideoPrompt = { shot_id, prompt }
VideoClip   = { shot_id, uri }
```

Provider-specific generation parameters, queue transport IDs, request fingerprints and worker
responses are operational state. They remain in `video_generation_manifest.json` rather than being
copied into `VideoClip`.

The application job identity is deterministic and independent of Salad transport identity:

```text
phase8-shot-<shot_id>-<plan_hash_prefix>
```

The hash covers the request-defining plan inputs: shot, generation profile, prompt, keyframe hash,
seed, dimensions, fps and LTX-valid frame count.

## Validated LTX production profile

The closed production profile is:

```text
task:               video.ltx25.generate
generation_profile: ltx25-distilled-a95ab856-fp8cpu-v1
resolution:         768x1280
fps:                24
quantization:       fp8-cast
offload:            CPU
GPU class:          RTX 5090
```

The worker loads the LTX-2.5 distilled model set directly from Python/PyTorch and keeps the pipeline
resident while one replica consumes multiple queue jobs sequentially.

The checkpoint paths are:

```text
diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
vae/ltx-2.5-video-vae-bf16.safetensors
vae/ltx-2.5-audio-vae-bf16.safetensors
latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
```

Phase 8 outputs are silent MP4 clips. Narration and final muxing belong to Phase 9.

## Validated Salad worker configuration

The real successful Phase 8.3/8.4 run established the configuration that must be reproduced by
`scripts/manage_phase8_worker.ps1 -Action Prepare`:

```text
worker group:          ai-video-factory-worker
queue:                 ai-video-factory-jobs
GPU class id:          851399fb-7329-4195-a042-d6514b28cf33
cpu:                   8
memory:                61440 MiB
shared memory:         8192 MiB
storage:               137438953472 bytes
priority:              medium
queue min replicas:    0
queue max replicas:    1
liveness path:         /health
liveness period:       30 s
liveness timeout:      10 s
liveness failures:     20
```

The tolerant liveness window is intentional. During real LTX pipeline construction and inference,
the earlier `timeout=5 / failure_threshold=3` setting killed otherwise healthy GPU instances. The
validated `timeout=10 / failure_threshold=20` configuration allowed the same warmed instance to
finish multiple real clips sequentially.

`Prepare` also injects `HF_TOKEN` from the process environment or a secure PowerShell prompt. No
Hugging Face, Salad, Postgres or R2 secret is stored in versioned files.

The validated Phase 8 worker image used during cloud closure was:

```text
docker.io/yagobordell/ai-video-factory@sha256:4577972ab55ecb8fdf305e87d3851b4db6d70b239ed3f61094142a7d7b8d0141
```

The successful live group reached version 11 before the complete 8-shot validation and was returned
to `stopped` with zero replicas afterward.

## Phase 8.3 real worker evidence

The single-shot smoke first proved the direct LTX path end to end:

```text
Salad queue
  -> production worker
  -> deterministic application job
  -> Postgres transactional claim/lease
  -> R2 keyframe input
  -> direct LTX inference
  -> R2 MP4 output
  -> persisted succeeded response
```

Shot 1 produced a real 768x1280 H.264 clip and a later resubmission of the exact logical request
returned `replayed=true` without incrementing the worker attempt count.

Shots 8 and 5 were then run sequentially on the same hot worker instance. That exercise proved that
the LTX pipeline could stay resident and serve more than one real generation before Phase 8.4 moved
to full storyboard fanout.

## Phase 8.4 complete storyboard validation

The canonical 8-shot validation used this run fingerprint:

```text
c90751bedd2dc65dac7ca7fba927b706ad514e1f72a91edc913c96465541e361
```

The eight deterministic application jobs were:

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

All eight transports reached `succeeded` with `submission_count=1`. Because shots 1, 5 and 8 were
identical to already completed Phase 8.3 application jobs, the worker returned them as idempotent
replays. Shots 2, 3, 4, 6 and 7 performed new real inference.

The final local media verification was:

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

Every clip passed:

- R2 response size vs local byte length;
- worker SHA-256 vs local SHA-256;
- exactly one H.264 video stream;
- 768x1280 dimensions;
- 24 fps;
- expected LTX-valid frame count;
- no audio stream.

The successful manifest SHA-256 was:

```text
73bb97a822e7c9e4bfe6e4ac79abf94a19424edab7c52d3675ce47e7a859609f
```

A second complete invocation reused the same manifest and finished with zero new submissions, zero
new transport IDs and the exact same manifest SHA-256. This closes application-level resume and
idempotence for the canonical storyboard run.

Detailed evidence is in [`phase8.4-validation-results.md`](phase8.4-validation-results.md).

## Production commands

The normal production orchestration flow is:

```powershell
# 1. Queue every unresolved canonical shot while the worker is stopped.
python scripts\run_phase8_videos.py --submit-only

# 2. Enable the existing autoscaled worker group.
.\scripts\start_phase8_autoscaled.ps1

# 3. Resume the manifest, observe status changes and fan in all completed clips.
python scripts\run_phase8_videos.py

# 4. Stop the group when the generation batch is complete.
.\scripts\manage_phase8_worker.ps1 -Action Stop
```

`run_phase8_videos.py` now watches the local manifest while it is waiting and prints state changes.
This is read-only progress reporting; queue polling and manifest ownership remain inside the workflow.
Use `--progress-seconds 0` when quiet output is preferred.

`Prepare` is not part of every run. It exists for intentionally publishing/upgrading the worker
image/configuration and must be executed only while the group is stopped:

```powershell
.\scripts\manage_phase8_worker.ps1 -Action Prepare
```

The command resolves the mutable build tag to an immutable image digest before patching Salad and
verifies the validated priority/liveness settings after the update.

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

`video_generation_manifest.json` is resumable operational state. `video_clips.json` is the canonical
handoff to the next media stage.

## Phase 9 handoff

Phase 9 must not depend on Salad transport state, GPU request schemas or LTX internals. Its useful
persisted inputs are:

```text
VideoClip[]
ShotTiming[]
NarrationAudio
NarrationWord[]
```

Conceptually:

```text
VideoClip[] + ShotTiming[]
            +
       NarrationAudio
            +
       NarrationWord[]
            |
            v
        Phase 9 compositor
            |
      +-----+-----------------------------+
      |     |             |               |
      v     v             v               v
   timeline transitions captions    overlays/motion graphics
      \     |             |               /
       +----+-------------+---------------+
                         |
                         v
                final muxed video
```

The compositor may probe/transcode individual clips as needed, but it should treat `VideoClip.uri`
and `shot_id` as the Phase 8 contract. Timing remains owned by `ShotTiming`; Phase 8's LTX frame
rounding is media-generation detail and must not redefine the canonical narration timeline.

## Closure decision

Phase 8 is closed because all of the following are now true:

1. motion is planned separately from static storyboard composition;
2. LTX-2.5 runs directly from Python/PyTorch with the validated distilled profile;
3. the production worker is transactional, leased and idempotent across Postgres/R2;
4. the same warmed real GPU worker completed multiple sequential jobs;
5. the complete storyboard can be fanned out before worker startup;
6. local orchestration resumes existing transports from an atomic manifest;
7. completed logical jobs replay instead of reinferring;
8. the full 8-shot run produced verified canonical `VideoClip[]` output;
9. a completed rerun created no new submissions or transports;
10. the successful cloud probe and priority settings are now encoded in operational tooling;
11. the GPU group was returned to stopped/zero replicas after validation;
12. Phase 9 can consume canonical local media contracts without depending on GPU transport state.

The next implementation phase is **Phase 9 — compositor**.
