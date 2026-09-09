# Phase 8.4 — Cloud validation results

Phase 8.4 was validated end-to-end on Salad on 9 September 2026 using the canonical 8-shot samurai production example.

This validation closes the fanout, resume and workflow-level idempotence gate introduced by `scripts/run_phase8_videos.py`.

## Validated production shape

The orchestrator used the existing Phase 8.3 GPU worker without changing its HTTP, Postgres or R2 contracts.

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

Operational state:

```text
video_generation_manifest.json
```

The manifest remained separate from the audiovisual domain contract and recorded one deterministic application job per shot plus the Salad transport state required for resume.

## Fanout evidence

The worker group was stopped before submission. A single invocation of:

```powershell
python scripts\run_phase8_videos.py --submit-only
```

created exactly eight queue transports before the GPU worker was enabled.

Run fingerprint:

```text
c90751bedd2dc65dac7ca7fba927b706ad514e1f72a91edc913c96465541e361
```

Application and transport jobs:

| Shot | Application job | Salad transport | Initial submissions |
| ---: | --- | --- | ---: |
| 1 | `phase8-shot-001-7ed73f1ff682` | `c01a44c8-95b7-4135-b194-ab083d124cf3` | 1 |
| 2 | `phase8-shot-002-8c1e5597aad9` | `fd1c4810-1b00-42e4-b2e5-e9aab767389e` | 1 |
| 3 | `phase8-shot-003-1fe7826f603e` | `91fbb366-8626-4053-acb0-dc3f4b70b91c` | 1 |
| 4 | `phase8-shot-004-b745637909c4` | `80e51253-c1b4-483a-a43c-9bdaf5d15099` | 1 |
| 5 | `phase8-shot-005-69262d45109b` | `d914bc4c-b503-4ca6-8f74-7b892579a7ba` | 1 |
| 6 | `phase8-shot-006-a1aa782f7543` | `057f8689-0d18-41e3-84da-2ec75f0e009f` | 1 |
| 7 | `phase8-shot-007-dac16fef77d7` | `9f5449ac-b841-44fa-9ed1-e82bc2c0be35` | 1 |
| 8 | `phase8-shot-008-a617c67ccd15` | `e80f3f80-6ea6-458a-9198-78c1a5f28779` | 1 |

All eight transports reached `succeeded`.

## Warm-worker execution

After bootstrap, one GPU instance consumed the queue sequentially.

The three application jobs already completed during Phase 8.3 were replayed instead of reinferred:

```text
shot 1 -> replayed=true
shot 5 -> replayed=true
shot 8 -> replayed=true
```

Shots 2, 3, 4, 6 and 7 performed real LTX-2.5 inference.

This demonstrated that the orchestrator can mix historical successes and new work in one fanout while preserving the worker's existing Postgres/R2 idempotence boundary.

## Fan-in evidence

The completed workflow wrote:

```text
data/output/phase8/video_clips.json
data/output/phase8/video_clips/shot_001.mp4
...
data/output/phase8/video_clips/shot_008.mp4
```

`video_clips.json` contained shots 1 through 8 exactly once and in canonical order.

Final media validation:

| Shot | Frames | Duration | Replay | SHA-256 prefix |
| ---: | ---: | ---: | --- | --- |
| 1 | 89 | 3.708 s | yes | `2f5d01380ecf` |
| 2 | 185 | 7.708 s | no | `da70e13fea89` |
| 3 | 129 | 5.375 s | no | `3175fb0f51f3` |
| 4 | 73 | 3.042 s | no | `25dd8ccf280e` |
| 5 | 233 | 9.708 s | yes | `51ba6580ecfa` |
| 6 | 161 | 6.708 s | no | `6030f589b6be` |
| 7 | 185 | 7.708 s | no | `72314f742619` |
| 8 | 65 | 2.708 s | yes | `4d15fe9c9662` |

Every clip passed:

- local byte-size verification against the persisted worker response;
- SHA-256 verification against R2/worker metadata;
- exactly one H.264 video stream;
- `768x1280` resolution;
- 24 fps;
- the expected LTX frame count for its canonical shot timing;
- no audio stream.

The local machine also wrote detailed ffprobe evidence under `data/output/phase8/validation/` and the aggregate local validation record at `data/output/phase8/phase8.4-validation.json`. These generated outputs remain intentionally unversioned.

## Workflow-level idempotence proof

After the first complete fan-in, `scripts/run_phase8_videos.py` was executed a second time with the same manifest.

The second invocation completed successfully with:

- the same eight `transport_job_id` values;
- `submission_count=1` for every shot;
- zero new submissions;
- zero new transport jobs;
- no polling of already completed jobs;
- the same ordered `VideoClip[]` output.

The manifest SHA-256 before and after the second invocation was identical:

```text
73bb97a822e7c9e4bfe6e4ac79abf94a19424edab7c52d3675ce47e7a859609f
```

This proves workflow-level resume/idempotence on top of the worker-level application-job replay already validated in Phase 8.3.

## Operational findings retained from Phase 8.3

The production Salad worker remained on the digest-pinned Phase 8 image:

```text
docker.io/yagobordell/ai-video-factory@sha256:4577972ab55ecb8fdf305e87d3851b4db6d70b239ed3f61094142a7d7b8d0141
```

The liveness probe used the validated tolerant settings introduced after repeated transformer-construction restarts:

```text
path=/health
period_seconds=30
timeout_seconds=10
failure_threshold=20
```

With those settings, the worker completed the 8-shot queue without container replacement during inference.

At the end of validation the worker group was stopped and returned to zero replicas.

## Closure gates

Phase 8.4 is closed because the real-cloud run proved all of the following:

1. one manifest represented all eight deterministic jobs;
2. full queue fanout occurred while the worker was stopped;
3. historical Phase 8.3 successes replayed rather than reinferred;
4. all remaining shots completed through the same worker contract;
5. fan-in produced eight ordered canonical `VideoClip` assets;
6. all local MP4s matched persisted size and SHA-256 metadata;
7. codec, dimensions, fps, frame counts and silence were verified;
8. a second completed invocation created zero new submissions and zero new transports;
9. the manifest remained byte-for-byte unchanged on the second invocation;
10. Salad returned to `stopped` with zero replicas.

## Next step

Phase 8.5 should perform the final Phase 8 closure: consolidate the Phase 8.1–8.4 evidence, update the global architecture/README status, record the final production commands and hand the canonical `VideoClip[]` batch to Phase 9 composition.
