# Isolated Salad I2V smoke and experimental camera profile

The LTX-2.5 worker has separate Salad groups and task-restricted PostgreSQL
polling for I2V (`ltx25_i2v`) and avatar A2V (`ltx25`). The code was merged
after two real I2V technical smokes; the experimental camera profile is **opt-in**.
**It reduces the unwanted zoom but does not eliminate it.** Neither the I2V
default nor the A2V `reference` profile is silently modified by camera guidance.

## Verified runs and immutable images

| Run | Runtime image (linux/amd64 manifest) | Technical result | Visual result |
| --- | --- | --- | --- |
| `i2v-smoke-003` | `sha256:bffdca9cfaf5e6e19cd9f20b7f88a562c3af8cf2854d7bad56e1f66700a87564` (v11) | MP4, 1280x720, 24 fps, 2.041667 s, no audio; one real Postgres submission | Unwanted camera push-in |
| `i2v-static-camera-001` | `sha256:6aed710bedc75aea072a40108a20887bbd578e7520d39dfe4d961e3e745a2bf9` (v12) | MP4, 1280x720, 24 fps, 2.041667 s, no audio; one real Postgres submission | Reduced but persistent zoom; **not fixed** |

The v12 result had SHA-256
`6e4a1399cd702a1022f21c68606bdb976071e606199b32e49b28f1c482440ecc`.
Both runs finished with the I2V group reporting `status=stopped` and
`pending=False`; observed technical success is not visual acceptance.
The v12 bootstrap wait was 3089.8 seconds, dominated by instance/network
selection and model preparation. This is not GPU inference time.

The v12 profile preserves the positive user prompt, seed, frame grid,
first-frame image strength 1.0, ancestral sampler, Stage 2 and safe
eager-SDPA DiffVAE decode. Only Stage 1 adds an independently pinned
camera-only negative prompt and effective video CFG 1.25 via
`GuidedDenoiser`. Its distinct generation profile and job ID prevent an
accidental R2 replay of the default baseline.

## Deployment boundary

**Merging code is not a deployment.** The tracked Salad service manifest
continues to list the historical v11 image tag. A manually prepared, stopped
I2V group can use the independently reviewed v12 pinned digest. Never
overwrite published v11/v12 tags or infer a digest from a mutable tag.

Do not start the new capacity controller until **both** I2V and A2V
groups have been prepared with task-restricted images and validated.
The previous A2V deployment may still be unrestricted; starting it while
I2V jobs exist could allow it to claim the wrong task. Preparing or
reconfiguring the A2V group is a separate controlled operation; this I2V
smoke must never start or stop the avatar group.

The manual capacity manager uses a PostgreSQL advisory lock. Stop the
singleton capacity controller through its documented procedure before
manual Salad capacity mutations. A stopped group showing
`replicas=1` has one *configured* replica, not a running billable worker.

## Re-run the opt-in comparison (Windows PowerShell)

Use a fresh RunId and the same baseline input files, prompt and duration;
keep `i2v-smoke-003` and its manifest, report and MP4 available locally.
The controlled script hashes and compares the baseline MP4 and PNG, and
compares prompt/timings **before** allocating GPU. It derives the same
effective seed from the baseline RunId but gives the new profile a distinct
application job identity. If any preflight fails, it does not start Salad.

```powershell
git switch main
git pull --ff-only
uv sync --locked --extra dev --python 3.12

$Tag = "docker.io/yagobordell/ai-video-factory:ltx25-i2v-static-camera-v12"
docker buildx imagetools inspect $Tag

$PinnedImage = "docker.io/yagobordell/ai-video-factory@sha256:6aed710bedc75aea072a40108a20887bbd578e7520d39dfe4d961e3e745a2bf9"

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\salad\manage_salad_worker.ps1 `
  -Action Status -Service ltx25 -EnvFile .env -NonInteractive

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\salad\manage_salad_worker.ps1 `
  -Action Status -Service ltx25_i2v -EnvFile .env -NonInteractive

# Only if the I2V group is stopped and is not already on the pinned digest:
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\salad\manage_salad_worker.ps1 `
  -Action Prepare -Service ltx25_i2v -Image $Tag `
  -PinnedImage $PinnedImage -EnvFile .env -NonInteractive

# This starts a paid I2V worker. A new RunId is required.
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\smoke\run_ltx25_i2v_smoke_controlled.ps1 `
  -Image ".\data\input\keyframe_i2v.png" `
  -Prompt "The camera remains fixed. The subject gently moves with subtle natural motion in one continuous shot." `
  -ExpectedPinnedImage $PinnedImage `
  -I2VProfile static-camera `
  -CompareWithRunId "i2v-smoke-003" `
  -DurationSeconds 2 -SeedBase 4242 `
  -RunId "i2v-static-camera-002" `
  -EnvFile .env -NonInteractive
```

Verify `status=stopped` for `ltx25_i2v` after the run. If cleanup
fails, stop **only** that group using
`manage_salad_worker.ps1 -Action Stop -Service ltx25_i2v`.
Review the actual MP4 for background stability, subject motion,
identity and distortions; ffprobe/hash/CI alone do not establish
camera stability. No further paid run is required to close this
experimental phase.
