# Isolated Salad image-to-video smoke (draft PR #238)

**Draft candidate; isolated v11 I2V has completed one technically valid real smoke, but the camera push-in failed visual acceptance.** The I2V and avatar jobs require separate
Salad container groups and task-restricted PostgreSQL polling. This test
must not change the existing avatar group, merge the PR, or start the global
capacity controller against an incomplete new manifest. The baked-model
experiment in PR #237 is out of scope. The opt-in static-camera experiment
requires a NEW immutable image; the earlier v11 digest cannot run its code.

## Prerequisites (Windows PowerShell)

Check out and install the PR branch before allocating GPU:

```powershell
git fetch origin
git switch feat/ltx25-i2v-isolated-salad-smoke
git pull --ff-only
uv sync --locked --extra dev --python 3.12

powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25 -EnvFile .env -NonInteractive
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25_i2v -EnvFile .env -NonInteractive
```

Stop the singleton Capacity Controller using its documented procedure before
manual Salad mutations. The manager's PostgreSQL advisory lock rejects
simultaneous controller ownership. Ensure the existing avatar group is
stopped or missing and no old I2V transports are running. The test checks
avatar status but does **not** stop, reconfigure or deploy that group.

## Historical v11 baseline (do not rebuild)

The published v11 `linux/amd64` image digest used for `i2v-smoke-003` was
`docker.io/yagobordell/ai-video-factory@sha256:bffdca9cfaf5e6e19cd9f20b7f88a562c3af8cf2854d7bad56e1f66700a87564`.
That run passed technical validation but showed a camera push-in. Do not
repush the v11 tag or reuse its digest for the camera-control experiment.
The tracked service manifest retains the v11 tag until the new image and
visual result are approved; a test-only `Prepare -Image $Tag -PinnedImage
$PinnedImage` can configure the isolated stopped I2V group without changing
production manifests.

## Opt-in static-camera comparison (new image required)

The original `i2v-smoke-003` with the v11 image produced one valid MP4, but
visually zoomed in despite the fixed-camera positive prompt. This branch adds
`--i2v-profile static-camera` behind an explicit opt-in smoke parameter.
Stage 1 uses the exact camera-only negative from the A2V `static-reference`
experiment with video CFG 1.25 and `GuidedDenoiser`, while keeping the original
positive prompt, image strength 1.0, ancestral sampler, Stage 2, model weights,
seed and safe eager-SDPA DiffVAE backend unchanged. The resident pipeline is
reused. This is **not** evidence that the zoom is fixed: the new guided Stage 1
may be slower or consume more VRAM. The normal I2V path stays upstream-distilled.

After CI is green on the final reviewed commit, build and push a **new tag**,
not the published `ltx25-isolated-tasks-v11` tag, and inspect its real
`linux/amd64` digest. Prepare ONLY the stopped I2V group with that immutable
digest; leave the avatar group unchanged. Preparing the group does not run
inference, but starting it can incur significant cold-model download time.
Do not reuse the old v11 pinned digest for the experiment.

When preparing the **new** candidate, use a fresh, never-reused tag.
The following is an operator example, NOT a completed build or deployment:

```powershell
$Tag = "docker.io/yagobordell/ai-video-factory:ltx25-i2v-static-camera-v12"
docker buildx build --platform linux/amd64 --file docker/workers/ltx25/Dockerfile --tag $Tag --push .
docker buildx imagetools inspect $Tag
$PinnedImage = "docker.io/yagobordell/ai-video-factory@sha256:REPLACE_WITH_REAL_LINUX_AMD64_MANIFEST_DIGEST"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\salad\manage_salad_worker.ps1 -Action Prepare -Service ltx25_i2v -Image $Tag -PinnedImage $PinnedImage -EnvFile .env -NonInteractive
```

For a fair one-job comparison with the already reviewed baseline, use the
existing `keyframe_i2v.png` and exactly the same positive prompt/duration,
then pass `-I2VProfile static-camera -CompareWithRunId "i2v-smoke-003"`
and a NEW `-RunId`. The wrapper checks the baseline MP4 hash, image,
prompt and timing before GPU allocation; it derives the **same effective
seed** from the baseline RunId, while the new generation profile creates a
different application job ID and R2 output. It stops only the I2V group in
`finally`. Never present a green ffprobe result as visual acceptance.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\smoke\run_ltx25_i2v_smoke_controlled.ps1 `
  -Image ".\data\input\keyframe_i2v.png" `
  -Prompt "The camera remains fixed. The subject gently moves with subtle natural motion in one continuous shot." `
  -ExpectedPinnedImage $PinnedImage `
  -I2VProfile static-camera `
  -CompareWithRunId "i2v-smoke-003" `
  -DurationSeconds 2 `
  -SeedBase 4242 `
  -RunId "i2v-static-camera-001" `
  -EnvFile .env `
  -NonInteractive
```

## One paid I2V generation

Use a real 1280x720 PNG or Qwen 1280x736 PNG. The script creates a fresh
single-shot manifest per `RunId`, preflights R2/source, verifies the exact
image digest and `LTX_WORKER_TASK_MODE=i2v`, checks two consecutive
`/ready` observations, submits via Phase 8/Postgres/R2, validates the
silent H.264 MP4 and stops only `ltx25_i2v` in `finally`.
Cold image/model download can be substantially longer than inference.
The wrapper derives a deterministic **effective seed** from `RunId` and
`SeedBase`; keep `RunId` unique for each fresh paid generation. A cached
R2 replay with no new Postgres submission is explicitly rejected.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke\run_ltx25_i2v_smoke_controlled.ps1 `
  -Image ".\data\input\keyframe.png" `
  -Prompt "The camera remains fixed. The subject gently turns their head and blinks. Natural subtle motion, one continuous shot." `
  -ExpectedPinnedImage $PinnedImage `
  -DurationSeconds 2 `
  -SeedBase 4242 `
  -RunId "i2v-smoke-001" `
  -EnvFile .env `
  -NonInteractive
```

Inspect `data/output/deployment-validation/ltx25-i2v/<RunId>/output/video_clips/shot_001.mp4`
and the adjacent `i2v-smoke-report.json`. Compare the MP4 visually with the
input PNG: confirm movement follows the prompt, stable identity and framing,
no severe geometry warping, static frames or unexpected camera motion.
Technical ffprobe/FFmpeg/hash checks do **not** qualify as visual acceptance.

Finally confirm the isolated I2V group is stopped and not billed, and the
avatar group stayed untouched. If cleanup fails, investigate and stop only
the I2V group:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25_i2v -EnvFile .env -NonInteractive
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\salad\manage_salad_worker.ps1 -Action Stop -Service ltx25_i2v -EnvFile .env -NonInteractive
```

Do not merge PR #238 until CI, real paid I2V generation, cleanup/billing
evidence and human visual review are all accepted. Before *production*
enablement, the old avatar group must also be updated to a tested
task-restricted image with `LTX_WORKER_TASK_MODE=a2v`. A legacy unrestricted
avatar worker can still claim I2V jobs if started.
