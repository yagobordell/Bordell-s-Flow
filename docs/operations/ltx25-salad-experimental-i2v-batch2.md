# EXCLUSIVE UNMERGED experiment: true one-GPU 2-video I2V

**DO NOT MERGE** and do not enable in production. This is an opt-in
video.ltx25.experimental_batch2 task in the isolated I2V worker, NOT automatic
coalescing of ordinary Phase 8 or pre-existing queue jobs. It uses one atomic
Postgres request, two independent PNG/prompt/seed triples, one FP8 transformer
and two SHA-verified MP4 outputs plus an explicit mode report.

The checkpoint, existing validated FP8 cast/no-offload I2V recipe, ancestral
sampler, 8+3 sigma schedule, single-prompt Gemma encoding, conditioning
strength, eager DiffVAE and FFmpeg encoding are unchanged. The supported
experiment is TWO native 1280x720 PNGs, 5 seconds, 24 fps, 121 frames. The
40-GiB-RAM / 32-GiB-VRAM Salad machine is unchanged.

The mode report distinguishes:
- batch2: genuine two-sample transformer forwards, shared model per stage;
- stage_shared_sequential_forward: one transformer build per stage, with
  two separately forwarded samples when B=2 ran out of VRAM;
- reference_sequential_oom_fallback: the exact original visually validated
  one-video generator run twice if stage sharing also ran out of VRAM.

No automatic fallback is allowed unless -AllowSequentialFallback is supplied.
A fallback MUST NOT be represented as successful GPU-parallel generation.

**Quality condition:** automatic codec, geometry, duration and SHA checks DO
NOT establish visual equivalence. Use the same images/prompts/seeds as the
approved single-video videos and compare each output side by side. Batch 2 may
change floating-point accumulation even when all generation parameters match.
Do not merge or switch production to batch2 unless both results meet quality
requirements and memory/timing benefits are actually observed on Salad.

## Build and prepare an experimental Docker image

Stop the Capacity Controller and confirm both LTX groups are stopped and the
I2V queue is empty. Run from the repository root in PowerShell:

~~~powershell
git fetch origin
git switch experiment/salad-ltx-a2v-resolutions-i2v-contention
git pull --ff-only
uv sync --locked --extra dev --python 3.12

$Tag = "docker.io/yagobordell/ai-video-factory:ltx25-batch2-$(Get-Date -Format yyyyMMddHHmmss)"
$Meta = Join-Path $env:TEMP "ltx25-batch2-build.json"
docker buildx build --platform linux/amd64 -f docker/workers/ltx25/Dockerfile -t $Tag --metadata-file $Meta --push .
if ($LASTEXITCODE -ne 0) { throw "Docker build/push failed" }
$Digest = (Get-Content $Meta -Raw | ConvertFrom-Json).'containerimage.digest'
$I2VPinnedImage = "docker.io/yagobordell/ai-video-factory@" + $Digest
docker buildx imagetools inspect $I2VPinnedImage
if ($LASTEXITCODE -ne 0) { throw "Pinned image unavailable" }
.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25 -EnvFile .env -NonInteractive
.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25_i2v -EnvFile .env -NonInteractive
.\scripts\salad\manage_salad_worker.ps1 -Action Prepare -Service ltx25_i2v -SkipBuild -PinnedImage $I2VPinnedImage -EnvFile .env -NonInteractive
~~~

Use ONLY the freshly built experimental digest. The previous production FP8
image cannot run the new paired task. Do NOT override a running controller.

## Controlled trial: reuse the visually good October 3 reference outputs

Replace these paths only if your prior experiment used different paths. The
previous queue run used seed 5242 for queued-a and 5243 for queued-b.

~~~powershell
$Previous = ".\data\output\deployment-validation\ltx25-i2v-queue\i2v-queue-20261003153846"
$RunPair = "i2v-batch2-$(Get-Date -Format yyyyMMddHHmmss)"
$Shared = @{
  ImageA = ".\data\input\i2v_a.png"
  PromptA = "The subject takes a small step forward while the camera stays fixed."
  ImageB = ".\data\input\i2v_b.png"
  PromptB = "The subject slowly turns their head as sunlight moves across the scene."
  SeedA = 5242
  SeedB = 5243
  ExpectedPinnedImage = $I2VPinnedImage
  RunId = $RunPair
  EnvFile = ".env"
  ReferenceA = "$Previous\queued-a\output\video_clips\shot_001.mp4"
  ReferenceB = "$Previous\queued-b\output\video_clips\shot_001.mp4"
}
.\scripts\smoke\run_ltx25_i2v_batch2_trial.ps1 @Shared -CheckInputsOnly
~~~

The last command validates local inputs, reference files and .env for FREE;
it makes NO Salad/R2 requests. Once that passes, run one paid experiment.
The recommended first attempt allows both transparent reduced-memory modes:

~~~powershell
.\scripts\smoke\run_ltx25_i2v_batch2_trial.ps1 @Shared -AllowSequentialFallback
~~~

For a strict genuine B=2 admission test, omit -AllowSequentialFallback. An
OOM will then explicitly FAIL rather than hiding a sequential result. The
script always tries to STOP its one Salad I2V GPU in finally.

Outputs:
data/output/deployment-validation/ltx25-i2v-batch2/<RunId>/
- batch2-a.mp4, batch2-b.mp4
- batch2-report.json and experiment-summary.json (actual mode, peak VRAM,
  failed attempts, independent seeds, technical validation)
- job-request.json and reference-provenance.json

Important Salad log: LTX25_I2V_PAIR_STAGE_METRICS. It records transformer
load seconds, denoising seconds and cgroup RAM, page-fault counters for EACH
stage. This directly helps investigate the prior 233-second transformer load
stall without increasing RAM.

Verify the group is STOPPED afterwards:

~~~powershell
.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25_i2v -EnvFile .env -NonInteractive
~~~

Require full CI GREEN, outputs at least as good as both reference MP4s by
human visual inspection, and measured performance/memory benefits on the
actual RTX 5090 before considering any future promotion. Leave PR unmerged.
