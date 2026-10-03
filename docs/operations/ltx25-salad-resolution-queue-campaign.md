# Salad campaign: two avatar resolutions and I2V queue contention (EXPERIMENT ONLY)

This work is on `experiment/salad-ltx-a2v-resolutions-i2v-contention`. **Do not merge or treat as production-approved** before branch CI, real 5090 tests and visual review pass. The branch adds experimental A2V 1920x1080 output; internally the LTX grid is 1920x1088, with four edge-extended rows per side and center-cropped output. An actual 1080p GPU success **has not yet been established**; OOM remains possible. The existing 1280x720 A2V behavior is unchanged.

**I2V caveat:** one current `ltx25_i2v` replica has a singleton Postgres job poller and an inference-mode lock; two simultaneous clients do NOT run two diffusion passes at once. The I2V campaign compares one baseline against two concurrent *submissions* on one GPU to measure queue behavior, cost and actual end-to-end elapsed time. This is not an implementation or validation of true GPU parallel generation. Do not modify the production scheduler or advertise a same-GPU throughput gain from this benchmark.

## One-time local checks (no paid GPU)

Open PowerShell in the repository checkout (not `main`):

```powershell
git fetch origin
git switch experiment/salad-ltx-a2v-resolutions-i2v-contention
git pull --ff-only
uv sync --locked --extra dev --python 3.12
uv run --locked --extra dev python -m pytest -q tests/unit/workers/test_ltx_a2v_1080_experiment.py tests/unit/workers/test_ltx_a2v_task.py tests/unit/workers/test_ltx_a2v_runtime.py tests/unit/workers/test_ltx_i2v_queue_campaign.py
.\scripts\salad\manage_salad_stack.ps1 -Action Validate
```

Provide two distinct real presenter images. Reuse the **same WAV or MP3** for both avatars (recommended for directly comparable 720p/1080p timing), ideally **5 seconds of speech**. Different audio files also work, but should have closely matching durations. The reference profile accepts at most 12 seconds per audio segment. Example files below should be replaced by real existing paths. Both A2V cases use the `reference` recipe and 24 fps; they are separate avatars, not resolution samples of the same identity.

Stop the singleton Capacity Controller first using `docs/operations/salad-capacity-controller.md`. The manager refuses concurrent manual mutations using its PostgreSQL advisory lock. Make sure there is no outstanding A2V/I2V work in the shared Postgres queue. Both Salad LTX groups must be stopped when beginning each campaign.

## A2V: build and pin NEW experimental worker (no GPU yet)

Build from this **exact branch**, push with a unique never-before-used Docker tag and prepare **only** the stopped A2V group:

```powershell
$Tag = "docker.io/yagobordell/ai-video-factory:ltx25-a2v-1080-experiment-$(Get-Date -Format yyyyMMddHHmmss)"
$BuildMeta = Join-Path $env:TEMP "ltx-a2v-1080-experiment.json"
docker buildx build --platform linux/amd64 -f docker/workers/ltx25/Dockerfile -t $Tag --metadata-file $BuildMeta --push .
if ($LASTEXITCODE -ne 0) { throw "Docker build/push failed" }
$Digest = (Get-Content $BuildMeta -Raw | ConvertFrom-Json).'containerimage.digest'
if ($Digest -notmatch '^sha256:[0-9a-f]{64}$') { throw "Missing published image digest" }
$A2VPinnedImage = "docker.io/yagobordell/ai-video-factory@${Digest}"
docker buildx imagetools inspect $A2VPinnedImage
if ($LASTEXITCODE -ne 0) { throw "Cannot inspect immutable published image" }

.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25 -EnvFile .env -NonInteractive
.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25_i2v -EnvFile .env -NonInteractive
.\scripts\salad\manage_salad_worker.ps1 -Action Prepare -Service ltx25 -SkipBuild -PinnedImage $A2VPinnedImage -EnvFile .env -NonInteractive
```

Only use `Prepare` with both groups idle and the controller stopped. The A2V pinned digest is local to this PowerShell session; keep it for the A2V trial.

## A2V paid session: 720p B, then 1080p A (same one GPU)

The script verifies distinct avatar images, valid speech files and durations, image pinning, R2, Python worktree and stopped group isolation **before starting Salad**. Reusing identical speech is permitted and recorded in the campaign summary.

First verify the local inputs **and that the local `.env` can be loaded**, without any Salad/R2 calls. Both MP3 paths deliberately point to the **same** file:

```powershell
.\scripts\smoke\run_ltx25_a2v_resolution_batch.ps1 `
    -Avatar720 ".\data\input\avatar_b.png" -Audio720 ".\data\input\speech.mp3" `
    -Avatar1080 ".\data\input\avatar_a.png" -Audio1080 ".\data\input\speech.mp3" `
    -ExpectedPinnedImage $A2VPinnedImage -Accept1080pOomRisk -CheckInputsOnly
```
 It starts A2V once, waits for `/ready`, runs 720p first and then experimental 1080p, preserving each result. A failure aborts remaining paid trials and still attempts `Stop` in a `finally` block.

```powershell
$RunA2V = "a2v-resolutions-$(Get-Date -Format yyyyMMddHHmmss)"
.\scripts\smoke\run_ltx25_a2v_resolution_batch.ps1 `
    -Avatar720 ".\data\input\avatar_b.png" -Audio720 ".\data\input\speech.mp3" `
    -Avatar1080 ".\data\input\avatar_a.png" -Audio1080 ".\data\input\speech.mp3" `
    -ExpectedPinnedImage $A2VPinnedImage `
    -Accept1080pOomRisk `
    -RunId $RunA2V -EnvFile .env
```

Inspect `data/output/deployment-validation/ltx25-a2v-resolutions/<RunId>/campaign.json`, both MP4s, `metadata.json` and ffprobe files. Check lip-sync, visual integrity and speech tail independently for both avatars; codec/metadata success does not imply good lip-sync. Record inference time, total generation time, wall time, peak VRAM, whether identical speech was used, and whether 1080p failed OOM. Confirm `ltx25` is stopped afterwards.

## I2V: pin the reviewed production FP8 image (no GPU yet)

The I2V campaign makes **no new I2V model changes**; use the reviewed production FP8 no-offload standard Docker image from your latest successful benchmark, not the experimental A2V image. Resolve its **actual digest** (never guess one), and prepare only the I2V group if its stopped instance currently uses another image.

```powershell
# Replace with the EXISTING successfully reviewed FP8 digest from your own prior build.
$I2VPinnedImage = "docker.io/yagobordell/ai-video-factory@sha256:REPLACE_WITH_64_HEX_CHARACTERS"
docker buildx imagetools inspect $I2VPinnedImage
.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25 -EnvFile .env -NonInteractive
.\scripts\salad\manage_salad_worker.ps1 -Action Prepare -Service ltx25_i2v -SkipBuild -PinnedImage $I2VPinnedImage -EnvFile .env -NonInteractive
```

Do not execute the placeholder digest. Leave A2V stopped. The manager's lock requires the Capacity Controller to remain stopped.

## I2V paid session: baseline, then two simultaneous requests

Use two 1280x720 PNGs (or 1280x736 Qwen outputs) with distinct prompts. The script prepares and hashes all inputs locally, validates stopped groups and digests, then starts **one** I2V replica. It generates one five-second I2V baseline and launches **two distinct clients simultaneously** with different seeds. The current worker processes their GPU jobs serially, so this measures queue overhead rather than actual parallel video generation. It stops its one GPU in `finally` and stores separate validated MP4s.

```powershell
$RunI2V = "i2v-queue-$(Get-Date -Format yyyyMMddHHmmss)"
.\scripts\smoke\run_ltx25_i2v_queue_campaign.ps1 `
    -ImageA ".\data\input\i2v_a.png" `
    -PromptA "The subject takes a small step forward while the camera stays fixed." `
    -ImageB ".\data\input\i2v_b.png" `
    -PromptB "The subject slowly turns their head as sunlight moves across the scene." `
    -ExpectedPinnedImage $I2VPinnedImage `
    -DurationSeconds 5 -RunId $RunI2V -EnvFile .env
```

Review `data/output/deployment-validation/ltx25-i2v-queue/<RunId>/campaign-results.json`, `capacity-lifecycle.json`, each subdirectory's `runner.log`, validated MP4 and report. Compare sequential versus two-client elapsed times. A baseline plus two queued jobs entails **three distinct paid I2V generations**, not two. This campaign is not proof of simultaneous inference.

After each campaign (also after failure), verify the relevant LTX group reports `status=stopped` and `pending_change=False`:

```powershell
.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25 -EnvFile .env -NonInteractive
.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25_i2v -EnvFile .env -NonInteractive
```

If cleanup fails, manually stop ONLY the affected group with `-Action Stop`, after verifying no pending shared jobs. Do not restart the Capacity Controller or merge until results and resource cleanup are reviewed.

## Acceptance before merge

- Branch CI green and targeted tests pass.
- Fresh A2V branch image is visibly digest pinned, A2V 720p and 1080p result reviewed (or 1080p failure explicitly reported; an OOM is *not* a 1080p validation).
- Both generated A2V videos use distinct avatars/audio, valid 24fps audio mux and acceptable lipsync.
- I2V baseline and both distinct queue-stress outputs are technically valid; no replay and exactly one Salad GPU replica for this campaign.
- Logs/metrics establish whether actual concurrency is available. Currently a different worker/inference architecture would be required to test actual same-GPU parallel diffusion safely; it is **not** part of this branch.

No code changes to `main` or automatic merge.
