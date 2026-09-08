# Phase 8.4 — Video fanout, resume and idempotence

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

The naming algorithm is the same one used by the Phase 8.3 smoke:

```text
phase8-shot-<shot_id>-<plan_hash_prefix>
```

The Salad transport job ID is deliberately not part of this identity. A failed/cancelled transport
can therefore be resubmitted while preserving the same application job. The worker's Postgres/R2
idempotency boundary remains authoritative for replay and conflict detection.

## Run fingerprint

Before submitting anything, Python builds every `GPUJobRequest` and hashes the ordered set of:

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

On restart:

| Manifest state | Resume behavior |
| --- | --- |
| `unsubmitted` | submit it |
| `pending` / `running` | query and continue the existing transport |
| `succeeded` + validated response | do not submit or query again |
| `failed` / `cancelled` | submit a new transport with the same application job ID |

A terminal failure reached during the current polling loop is persisted and reported. It is not
silently resubmitted forever. Re-running the command performs the explicit resume/retry step.
`--no-retry-terminal` can be used when an operator wants to inspect failures without resubmitting
them.

## Fan-in and artifact verification

After every transport succeeds, the orchestrator validates that each `GPUJobResponse` matches the
planned application job, request SHA-256 and deterministic output key.

The R2 MP4 is downloaded to `video_clips/shot_NNN.mp4`. Existing local files are reused only when
both their byte length and SHA-256 match the worker response. Otherwise they are downloaded again
and verified.

Only after all clips validate does the CLI write the ordered canonical `video_clips.json` batch.

Phase 9 therefore consumes local canonical clip URIs and does not need Salad transport state.

## Commands

Required process environment variables are the same as the Phase 8.3 smoke:

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

This is useful while the Salad worker group is stopped:

```powershell
python scripts\run_phase8_videos.py --submit-only
```

The command uploads missing content-addressed keyframes, submits every unresolved shot and writes
`video_generation_manifest.json`.

Enable the autoscaled worker separately. Queue demand will create the GPU replica.

### Resume and wait for fan-in

```powershell
python scripts\run_phase8_videos.py
```

The same command is safe to rerun after a local timeout, terminal transport failure or process
interruption. It resumes from the manifest instead of starting a new logical run.

The default full-run timeout is six hours and can be changed with `--timeout-seconds`.

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

## Cloud validation gate

Phase 8.4 code is complete when CI passes. Its real-cloud closure should then prove, using the
8-shot canonical example:

1. all eight deterministic jobs are represented in one manifest;
2. `--submit-only` creates queue fanout before the worker starts;
3. previously successful Phase 8.3 application jobs replay instead of reinferring;
4. the remaining shots complete through the same worker contract;
5. interrupting/restarting the local orchestrator preserves existing transport IDs;
6. a second completed invocation performs zero new submissions;
7. all eight R2 artifacts download with matching SHA-256;
8. `video_clips.json` contains shots 1..8 exactly once and in order;
9. the Salad group returns to zero replicas/stopped after validation.

That cloud exercise is the handoff from Phase 8.4 implementation to final Phase 8
validation/closure.
