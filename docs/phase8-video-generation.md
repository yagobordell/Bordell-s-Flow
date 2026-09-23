# Phase 8.4 — Video fanout, resume and idempotence

Estado: **cerrada y validada en cloud con el ejemplo canónico de 8 shots**.

Phase 8.4 moves video generation from the single-shot cloud smoke used in Phase 8.3 to the
production orchestration shape for a complete storyboard.

The GPU worker contract does not change. The orchestrator submits one deterministic
`video.ltx25.generate` application job per canonical shot and persists enough transport state to
resume an interrupted run without repeating completed work.

## Inputs and canonical output

Inputs:

```text
StoryboardKeyframe[]
VideoPrompt[]
ShotTiming[]
```

Canonical output:

```text
VideoClip = { shot_id, uri }
```

A complete run writes:

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

`video_generation_manifest.json` is operational resume state, not an audiovisual domain contract.
It records the immutable run fingerprint, deterministic application job ID, request SHA-256,
current Salad transport ID/status, submission count and validated worker response for each shot.

## Deterministic application jobs

Each shot derives its application job ID from:

- shot ID;
- generation profile;
- motion prompt;
- keyframe SHA-256;
- seed;
- width and height;
- fps;
- LTX-valid frame count.

The naming algorithm is shared by the Phase 8.3 smoke and Phase 8.4 workflow through
`ltx_video_application_job_id()`:

```text
phase8-shot-<shot_id>-<plan_hash_prefix>
```

The Salad transport job ID is deliberately not part of this identity. A failed/cancelled transport
can therefore be resubmitted while preserving the same application job. The worker's Postgres/R2
idempotency boundary remains authoritative for replay and conflict detection.

## Run fingerprint

Before submitting anything, Python builds every `InferenceJobRequest` and hashes the ordered set of:

```text
shot_id + application_job_id + request_sha256
```

That hash becomes the manifest `run_fingerprint`.

If a later invocation changes any request-defining input, the existing manifest is rejected instead
of mixing outputs from two generation plans. Archive or remove the old manifest intentionally before
starting a different plan.

## Fanout

For a fresh run the workflow:

1. validates exact ordered shot alignment across keyframes, prompts and timings;
2. validates a contiguous positive-duration timeline;
3. resolves and hashes every local keyframe;
4. ensures each content-addressed keyframe exists in R2;
5. creates the manifest;
6. submits **all unresolved shots** to Salad;
7. only then enters the polling loop.

This keeps queue demand visible to the autoscaler and allows a single warmed GPU replica to consume
multiple clips sequentially without scaling to zero between individual submissions.

## Resume rules

Every manifest mutation is written atomically using a temporary file plus `os.replace()`.

| Manifest state | Resume behavior |
| --- | --- |
| `unsubmitted` | submit it |
| `pending` / `running` | query and continue the existing transport |
| `succeeded` + validated response | do not submit or query again |
| `failed` / `cancelled` | submit a new transport with the same application job ID on the next explicit rerun |

A terminal failure reached during the current polling loop is persisted and reported. It is not
silently resubmitted forever. `--no-retry-terminal` lets an operator inspect recorded failures
without resubmitting them.

The manifest closes the normal local-process restart path. A process that starts with persisted
`pending`/`running` transport IDs resumes those exact Salad transports.

There remains an unavoidable narrow crash window between Salad accepting a POST and local manifest
persistence. If the client process dies in that interval, a later rerun may create a second transport
for the same deterministic application job. Worker-level Postgres/R2 idempotence still prevents a
second logical inference/foreign overwrite for an already completed application request.

## Progress output

`run_phase8_videos.py` watches the atomic manifest locally while `run_video_generation()` performs
queue polling. When a shot changes state it prints the complete current status table, for example:

```text
progress:
  shot=1 status=succeeded submissions=1 transport_job_id=...
  shot=2 status=running submissions=1 transport_job_id=...
  shot=3 status=pending submissions=1 transport_job_id=...
```

This watcher is read-only and performs no additional Salad API requests. Use:

```powershell
python scripts\run_phase8_videos.py --progress-seconds 0
```

to disable progress output.

## Fan-in and artifact verification

After every transport succeeds, the orchestrator validates that each `GPUJobResponse` matches the
planned application job, request SHA-256 and deterministic output key.

The R2 MP4 is downloaded to `video_clips/shot_NNN.mp4`. Existing local files are reused only when
both their byte length and SHA-256 match the worker response. Otherwise they are downloaded again
and verified.

Only after all clips validate does the CLI write the ordered canonical `video_clips.json` batch.

Phase 9 therefore consumes local canonical clip URIs and does not need Salad transport state.

## Commands

Required process environment variables:

```text
SALAD_API_KEY
SALAD_ORGANIZATION
SALAD_PROJECT
SALAD_QUEUE_NAME         # optional; defaults to ai-video-factory-jobs
R2_ENDPOINT_URL
R2_BUCKET
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
```

Do not store secrets in versioned files.

### Submit the complete fanout without waiting

With the worker group stopped:

```powershell
python scripts\run_phase8_videos.py --submit-only
```

The command uploads missing content-addressed keyframes, submits every unresolved shot and writes
`video_generation_manifest.json`.

### Enable the existing autoscaled worker

```powershell
.\scripts\start_phase8_autoscaled.ps1
```

With `min_replicas=0`, queue demand creates the GPU replica.

### Resume and fan in

```powershell
python scripts\run_phase8_videos.py
```

The same command is safe to rerun after a local timeout, terminal transport failure or process
interruption. It resumes from the manifest instead of starting a new logical run.

### Stop the group

```powershell
.\scripts\manage_phase8_worker.ps1 -Action Stop
```

`Prepare` is reserved for an intentional image/configuration upgrade and is not part of each batch.

## Unit validation

`tests/test_video_generation_workflow.py` covers:

- deterministic planning and LTX frame rounding;
- exact input alignment validation;
- true fanout before polling;
- resume of existing transports;
- no resubmission of succeeded jobs;
- selective resubmission of terminal failures using the same application job ID;
- persisted terminal failures for a later resume;
- rejection of a changed plan against an existing manifest;
- local clip SHA/size verification during fan-in.

## Real cloud validation

The Phase 8.4 closure run used:

```text
run_fingerprint=c90751bedd2dc65dac7ca7fba927b706ad514e1f72a91edc913c96465541e361
```

`--submit-only` created all eight transports while the worker was stopped. After enabling the group,
one warmed RTX 5090 instance consumed the queue.

All eight jobs reached `succeeded` with `submission_count=1`.

Previously completed Phase 8.3 application jobs replayed:

```text
shot 1 -> phase8-shot-001-7ed73f1ff682 -> replayed=true
shot 5 -> phase8-shot-005-69262d45109b -> replayed=true
shot 8 -> phase8-shot-008-a617c67ccd15 -> replayed=true
```

Shots 2, 3, 4, 6 and 7 performed new real inference.

The final clips contained the expected frame counts:

```text
shot 1:  89
shot 2: 185
shot 3: 129
shot 4:  73
shot 5: 233
shot 6: 161
shot 7: 185
shot 8:  65
```

Every local MP4 matched the worker size/SHA-256 metadata and passed ffprobe validation for H.264,
768x1280, 24 fps and zero audio streams.

A second completed invocation created:

```text
new submissions:   0
new transports:    0
```

and preserved the exact manifest SHA-256:

```text
73bb97a822e7c9e4bfe6e4ac79abf94a19424edab7c52d3675ce47e7a859609f
```

The Salad worker group was returned to `stopped` with zero replicas.

Detailed results are in [`phase8.4-validation-results.md`](phase8.4-validation-results.md). The final
Phase 8 architectural/operational closure is in [`phase8-closure.md`](phase8-closure.md).
