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


## Narrow follow-up objective (I2V only)

The final target is 1080p delivery for both I2V and A2V; **only I2V**
should execute TWO video clips in the same GPU batch. A2V continues
ONE video per job and its separate optimization work is NOT changed here.

The original successful 720p pair stays the DEFAULT isolated test. Only
after separately validating the I2V 1080p single-video memory/quality
profile should an operator schedule a dedicated paired full-HD trial.
This new I2V-only trial accepts exactly two NATIVE 1920x1080 PNG inputs,
two separate prompts/seeds and the unchanged five-second 24fps FP8 recipe.
Its parameters require explicit `accept_full_hd_batch2_oom_risk=true`,
which is deliberately absent by default. Example power-shell-only
local preflight (no Salad started):

```powershell
.\scripts\smoke\run_ltx25_i2v_batch2_trial.ps1 @Shared `
    -Width 1920 -Height 1080 `
    -AcceptFullHdBatch2OomRisk -CheckInputsOnly
```

A separate fresh immutable 1080p Docker image, new RunId and a deliberate
paid GPU approval are necessary for a real hardware test. NEVER run the
paid 1080p trial accidentally during the already verified 720p benchmark.
A full-HD paired trial refuses sequential fallback and unverified shared
stage-resident transformer weights; an OOM is visible rather than hidden
by a downgrade. **There is no evidence yet that native B=2 1080p fits in
the existing 32-GiB RTX 5090.** Keep production B2 routing to ordinary
single-video requests until the GPU test and visual review actually pass.

No A2V worker source is changed as part of this follow-up. This historical
combined experimental PR still contains prior A2V changes and has diverged
from the concurrently optimized A2V base; it must NOT be merged wholesale.
Reconcile/split I2V-only work after the A2V work is complete.

## Full-HD VRAM mitigation — NOT YET GPU VALIDATED

The 1080p paid B=2 trial OOMed **inside the second transformer stage**
after Gemma, first-stage B=2 diffusion and independent upsampling
succeeded. The latest I2V-only follow-up therefore runs full-HD B=2
with LTX's upstream FP8_CAST-compatible DISK block-streaming stage
(`DISK_CPU_SLOTS=2` for bounded pinned host RAM). Full GPU
weight residency remains the DEFAULT for the successful 720p test.

For full HD, obsolete stage1 conditioning, independent prompt-output
contexts and original stage1 video latent are explicitly released
before full-size stage2 construction. The required original stage1
audio latent and independent upscaled video latents stay intact.
The code trims only dead CUDA allocator cache and emits
`LTX25_I2V_PAIR_MEMORY_POLICY`,
`LTX25_I2V_PAIR_GPU_CHECKPOINT` (including before, after and failed
stage2 B=2 forwards) and the existing error snapshot. No denoising
equation, model dtype, seed, FPS, frame count, video resolution, or
GPU forward batch size is reduced. Disk streaming may greatly
increase read I/O and generation wall time.

The mitigation is a source-level candidate, **not** a proven 32-GiB
VRAM fit. Before another paid test, require free smoke preflight
and targeted I2V CI on the NEW immutable Docker digest. Do not
repeat the failed image, activate shared-stage transformer reuse
or enable sequential fallback. Keep A2V optimization and files
in their independent work branch.

## Quality-preserving latency changes (experimental, NOT approved without GPU trial)

**Important October 3 OOM postmortem:** the NEW shared-Gemma implementation
never reached transformer generation: the first attempt began the shared
prompt phase, OOMed, and restarted B=2; the second attempt OOMed while
copying Gemma weights to CUDA (traceback in safetensors loader). It is NOT
evidence that resident transformer failed; even the nonresident fallback
ran out of memory before either diffusion stage. Therefore the experimental
shared-Gemma optimization has been ROLLED BACK. Both prompts again use
the COMPLETE original upstream PromptEncoder, including separate image
resolution, single-prompt call, text encoder disposal, embeddings
processor and allocator trim BETWEEN videos. Do not assume these changes
fully resolve the OOM without a GPU validation run.

Each diffusion stage still builds the video encoder once, passing image A
and image B separately through the approved one-image conditioning
function. The earlier OOM-fallback bug that kept BOTH stages' image
conditionings resident is also fixed: the visually approved two-build
B=2 path builds stage2 conditioning only AFTER stage1 and upsample.

By default, the experimental pair job uses the **previously successful
two-build B=2 stage path**. The unproven single-build optimization is
now disabled by default: activate it only with `-EnableStageReuse` after
reviewing this OOM and arranging an explicitly controlled GPU test. When
enabled, it tries to **keep one FP8 transformer on GPU from stage 1
through upsample and stage 2**. The existing
B=2 GPU kernels, both independent RNG streams, sigmas and latents are
unchanged. Keeping transformer weights resident while VAE upsampling MAY
exhaust 32 GiB VRAM. If it does, the worker automatically frees the failed
attempt and retries the previously visually approved **two-transformer-build
B=2** recipe, even when sequential fallback was NOT enabled. Only an
explicit -AllowSequentialFallback permits subsequent B=1 downgrade. Never
claim the faster one-build optimization if the report shows the recovery path.

The report's truthful per-run fields:
- mode=batch2 and actual_gpu_batch_size=2: genuine one-GPU batch2;
- shared_transformer_stage_builds=1 and transformer_reused_between_stages=true:
  the optimized single-build batch2 survived both stages;
- shared_transformer_stage_builds=2 and transformer_reused_between_stages=false:
  the original approved GPU batch2 two-load stage path ran;
- failed_attempts: OOMs and whether an optimized shared-stage attempt was
  retried. Use elapsed_seconds AND transformer stage logs, not just mode.

**Control experiment:** the safe default is the two-build B=2 path with
original per-prompt Gemma disposal. A strictly opt-in trial of single-
build transformer GPU residency requires `-EnableStageReuse` and a new
RunId with the exact same inputs/prompts/seeds. The legacy flag
`-DisableStageReuse` is still accepted but is now redundant by default.
Do not claim a speedup or GPU-memory fit without actual measurements.
An additional unmodified old-Docker reference is needed to isolate *all*
changes; the previously approved visual October 3 pair is the reference.
Avoid paying for another trial until CI is green and a new pinned image
has been built and prepared while Salad is stopped.

The worker now logs LTX25_I2V_PAIR_PROMPT_BASELINE_METRICS,
LTX25_I2V_PAIR_SHARED_TRANSFORMER_LOADED and
LTX25_I2V_PAIR_STAGE_METRICS. Process counters under /proc/self provide
RSS, major/minor page faults and physical read bytes when Salad masks
cgroup memory paths; cgroup v1 and v2 counters are also supported.
The first transformer load may still be limited by actual storage/network
throughput. No code can guarantee to eliminate that cost without changing
how the underlying 42-GB BF16 checkpoint is stored or loaded. A separate
lossless checkpoint-cache experiment would require its own integrity and
quality gates; this branch deliberately does not alter weights/precision.


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
  SeedA = 5243
  SeedB = 5244
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
The recommended next controlled run should NOT begin until the old Salad
worker logs are reviewed. The safe two-build B=2 mode is now the default;
for a strict true B=2 run omit sequential fallback:

~~~powershell
.\scripts\smoke\run_ltx25_i2v_batch2_trial.ps1 @Shared
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
