# Isolated Salad image-to-video smoke (draft PR #238)

**Candidate only; not deployed.** The I2V and avatar jobs require separate
Salad container groups and task-restricted PostgreSQL polling. This test
must not change the existing avatar group, merge the PR, or start the global
capacity controller against an incomplete new manifest. The baked-model
experiment in PR #237 is out of scope.

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

Build and publish the reviewed PR source under a **new candidate tag**:

```powershell
$Tag = "docker.io/yagobordell/ai-video-factory:ltx25-isolated-tasks-v11"
docker buildx build --platform linux/amd64 --file docker/workers/ltx25/Dockerfile --tag $Tag --push .
docker buildx imagetools inspect $Tag
```

Take the **actual 64-hex published SHA-256 image digest** from inspect;
never use a mutable tag or the placeholder in a paid test. Prepare *only*
the isolated I2V group while stopped:

```powershell
$PinnedImage = "docker.io/yagobordell/ai-video-factory@sha256:REPLACE_WITH_REAL_64_HEX_DIGEST"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\salad\manage_salad_worker.ps1 -Action Prepare -Service ltx25_i2v -PinnedImage $PinnedImage -EnvFile .env -NonInteractive
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25_i2v -EnvFile .env -NonInteractive
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
