# LTX 2.5 I2V: instrumented optimization experiment

This experiment is **not a production rollout**. It changes neither the default
`generation_profile` nor the tracked Salad image/digest. Run controlled paid
RTX 5090 tests before approving the experimental profile.

## Independent recipes

| CLI `--i2v-profile` | CFG Stage 1 batch | Final audio decode | DiffVAE |
|---|---:|---|---|
| `default` | 1 | enabled (historical) | eager |
| `cfg-b2` | 2 | enabled | eager |
| `cfg-b2-silent` | 2 | skipped | eager |
| `cfg-b2-triton` | 2 | skipped | Triton if available, eager if not |
| `static-camera` | 1 | enabled | eager |

All recipes retain distilled 8+3 steps, positive and negative prompts,
effective negative CFG 1.25, joint audio/video diffusion, original seed,
conditioning strength 1.0, 1280x768 internal generation, 1280x720 delivery,
24fps and silent H.264 MP4 output.

`cfg-b2` isolates the effect of CFG batching; `cfg-b2-silent` isolates
the removal of the final discarded audio decoder; `cfg-b2-triton` isolates
the DiffVAE backend. Each has a **different application job identity / R2
key** so it cannot replay another recipe's cached result.

## Observability

`LTX25_I2V_PHASE_METRICS` logs one JSON record per completed generation:
pipeline acquisition, prompt encoding, two image conditioning phases,
two stage durations, spatial upsampling, lazy video decode (during encode),
optional final audio decode, encode including decode, estimated encode
excluding decoder, full time, observed peak CUDA allocation and reservation,
requested/effective decoder backend, stage-1 batch, the **expected** transformer
forward counts (not direct instrumentation), pipeline reuse and geometry.

CUDA synchronization is enabled for comparable phase timings. This changes
timing overhead but not model inputs or random state. Encode-exclusive is a
derived approximation, not an independently profiled FFmpeg/kernel measurement.
Timing of internal model-context setup is included in each stage total, not
separately attributed.

## Safety and rollout

- NATTEN is **always disabled** due to the previously observed 5090 segmentation
  fault. The experimental request checks Triton availability *before* building
  the pipeline. If unavailable, the effective backend is logged as eager.
- A Triton kernel failure during GPU work is **not** caught and retried in the
  same CUDA process: the existing GPU-failure handling invalidates the affected
  pipeline for normal retry. An eager-only profile is the rollback.
- Only one I2V pipeline is retained at a time. Changing requested decoder
  backend rebuilds it from locally cached checkpoints; benchmark that startup
  separately from steady-state generation.
- Stage 1 batch 2 can increase peak activation VRAM and cause OOM. Keep batch 1
  as the stable baseline and never auto-promote batch 2 on an untested 5090.
- The transformer still jointly generates video and audio latents. The silent
  optimization skips only the final waveform decoder that was previously
  discarded. The MP4 format remains silent.

## Controlled validation

Use a reviewed image pinned by digest **built from this branch**, keeping the
A2V Salad group stopped. The existing smoke script supports `-I2VProfile`
`default`, `cfg-b2`, `cfg-b2-silent` and `cfg-b2-triton`. Use
`-CompareWithRunId` to reuse baseline input image, prompts, timing and seed.

Run baseline and each experimental profile on the same real RTX 5090, first as
short 2-second smokes, then repeated 5-second comparisons. Check per-phase
latency and p50/p90 total, 0 OOM/segfault, observed decoder backend and VRAM.
Review generated clips side-by-side for subject/face/hands, background,
camera zoom and flicker. Validate ffprobe 1280x720/24fps/silent H.264, the
complete keyframe frame and subsequent 2560x1440 upscale. Running offline
pytest and CI does **not** validate visual quality or live Triton kernels.

Do not merge, change the production `generation_profile` or update Salad
manifests until controlled GPU/visual sign-off.


## One-GPU consecutive benchmark (recommended)

Do not repeat the old one-profile controlled smoke three times. The batch
wrapper scripts/smoke/run_ltx25_i2v_benchmark_batch.ps1 starts exactly one
pinned I2V Salad group, waits for readiness once, executes all profiles
sequentially with the same PNG, prompt, duration and effective seed, and stops
the group once in a finally block. The A2V group and the singleton Capacity
Controller must remain stopped during this manual experiment.

To reuse an already successful 2-second baseline without paying to generate
it a second time, retain its input files, validated MP4, technical receipt
and Postgres manifest. Export the FULL Salad log CSV containing the original
LTX25_I2V_PHASE_METRICS and INFERENCE_JOB_TASK_DONE messages. Use the exact
positive prompt and exact PNG from the successful original run.

~~~powershell
git pull --ff-only
uv sync --locked --extra dev --python 3.12

# Keep the real reviewed digest saved from the previous experimental Docker image.
$PinnedImage = "REPLACE_WITH_THE_PREVIOUS_REVIEWED_REPO_AT_SHA256_DIGEST"
$Image = ".\data\input\keyframe_i2v_1280x720.png"
$Prompt = "REPLACE_WITH_THE_EXACT_ORIGINAL_BASELINE_PROMPT"
$BaselineCsv = "C:\Users\User\Downloads\FULL_SALAD_LOG_EXPORT.csv"

$Options = @{
    Image = $Image
    Prompt = $Prompt
    ExpectedPinnedImage = $PinnedImage
    BaselineRunId = "ltx-base2-20261001203930"
    BaselineLogsCsv = $BaselineCsv
    DurationSeconds = 2
    SeedBase = 4242
    EnvFile = ".env"
    NonInteractive = $true
}
.\scripts\smoke\run_ltx25_i2v_benchmark_batch.ps1 @Options
~~~

Before starting billable capacity the wrapper validates: account and R2
configuration, immutable I2V image, the existing original MP4 hash and
Postgres receipt, SHA256 equality of all three input files, avatar group
isolation, and a stopped I2V group. It imports the original complete
per-phase record from CSV and then runs cfg-b2, cfg-b2-silent and
cfg-b2-triton consecutively on one GPU. The original baseline is NOT
regenerated. To test a new duration without a verified existing baseline,
omit BaselineRunId and BaselineLogsCsv: the wrapper runs all four profiles,
including the new default baseline, in one capacity session.

Results are saved under
data/output/deployment-validation/ltx25-i2v-batches/<batch-id>.
batch-manifest.json records the one-time bootstrap time and immutable image.
Every new profile subdirectory contains original inputs, the separate MP4,
i2v-smoke-report.json, run-result.json (UTC start/end and client wall time)
and phase-metrics.json (full CUDA-synchronized phase record, memory peaks,
backend, worker task time and raw matching Salad log events).
summary.csv and summary.json are rewritten after EACH finished profile:
earlier results survive later failures. The imported baseline also receives
a metrics record and summary row.

The log collector uses SALAD_ORGANIZATION and SALAD_PROJECT from the operator
environment and requires one genuine phase metric and the completed job
event for each profile. Missing or ambiguous metrics fail the run rather
than silently substituting a guess. Baseline wall time can be null because
the historical CSV records worker timings, not the local client's wait.
Compare bootstrap, client wall time, per-phase time, and pipeline_reused
separately. Switching to Triton may rebuild the decoder pipeline, so
cold-build time is not a fair proxy for warmed kernel speed.

On controlled errors the wrapper stops the dedicated group in finally.
An abrupt terminal kill or machine shutdown still requires a manual
Salad status check and possibly a manual stop. Keep the PR in draft and
do not merge until real GPU and visual comparison have passed.


## Conservative production I2V optimization: no unused audio decode

The production I2V *visual* recipe is unchanged: the default profile ID,
positive prompt, negative guidance, seed, stage-1 CFG max batch size 1,
stage-1 and stage-2 samplers, both video/audio diffusion latents,
DiffVAE eager decoder, frame count and output MP4 encoding are unmodified.
The only production change is skipping the **final decoded waveform**, which
used to be computed after both visual diffusion stages and then discarded;
the output encoder has always received audio=None. The 2-second CFG-b2
versus CFG-b2-silent real GPU experiment generated byte-identical MP4s,
supporting that this final waveform is not part of video generation.
A2V generation, which needs audio, is completely unchanged.

Do not turn on CFG batch 2 or Triton for normal runs yet. Keep those profiles
explicit experimental choices. Do not change the default profile ID or its
historical Postgres/R2 replay identity for a post-diffusion no-op on video.
Do not treat the earlier cold-start versus warm-run speed difference as
a measured CFG batch-2 improvement.

I2V phase metrics now reset CUDA allocator peaks before each inference job.
The JSON includes cuda_peak_reset=true when successful. Previously saved
cross-profile equal peaks may represent cumulative PyTorch allocator peaks
and should not be interpreted as equal true per-run memory use.
pipeline_reused still means a Python pipeline object already exists;
pipeline_prior_successful_generations and pipeline_known_warm distinguish
that condition from a pipeline that has completed previous inferences.
A fresh CUDA process or a pipeline object that is merely prepared can still
lazily load transformer/text-encoder weights during its first generation.
Neither new metric changes tensors, samplers or seeds.

### Mandatory fresh-generation quality gate before merge

Offline/CI tests assert the preserved visual recipe and execution order;
they CANNOT prove that a changed Docker image renders a byte-identical
production MP4 on Salad. The experimental branch includes an explicit
default-silent-verify profile: its *only* difference from default is an
independent application job identity so Postgres/R2 cannot replay the
historical MP4 instead of running the updated code.

After CI is green, build and publish a **new immutable I2V Docker image
from the branch containing the audio-skip change**, review its digest,
prepare the dedicated stopped I2V group with that digest, and stop the
Capacity Controller. The pre-change original reference must remain at
data/output/deployment-validation/ltx25-i2v/ltx-base2-20261001203930
and retain its real original MP4 and technical report. Run the following
only when intentionally approving a new **paid** GPU validation:

~~~powershell
$BaseRun = "ltx-base2-20261001203930"
$BaseFolder = ".\data\output\deployment-validation\ltx25-i2v\$BaseRun"
$Image = Join-Path $BaseFolder "inputs\keyframe.png"
$Prompt = "A woman gently applies moisturizer to her cheek. Natural hand movements, stable camera, realistic facial expressions, no camera zoom."
$PinnedImage = "REPLACE_WITH_NEW_REVIEWED_DOCKER_REPOSITORY_AT_SHA256_DIGEST"
$VerificationId = "ltx-default-silent-verify-$(Get-Date -Format yyyyMMddHHmmss)"
$Options = @{
    Image = $Image
    Prompt = $Prompt
    ExpectedPinnedImage = $PinnedImage
    I2VProfile = "default-silent-verify"
    CompareWithRunId = $BaseRun
    RequireExactBaselineVideoSha = $true
    DurationSeconds = 2
    SeedBase = 4242
    RunId = $VerificationId
    EnvFile = ".env"
    NonInteractive = $true
}
.\scripts\smoke\run_ltx25_i2v_smoke_controlled.ps1 @Options
~~~

The smoke checks SHA256 equality of the original input PNG, positive prompt
JSON and shot timings **before** starting the GPU. It computes the baseline
run's same effective seed; the verify profile uses the same negative guidance,
CFG=1 and eager decoder while forcing a fresh Postgres job key. After
independently validating the fresh silent MP4 it writes
quality-equivalence.json and REQUIRES its full MP4 SHA256 to match the
original historical pre-change MP4. Mismatch fails the quality gate and
stops Salad in finally; DO NOT merge or promote. Inspect the updated phase
JSON (audio_decode_enabled=false, cuda_peak_reset status, exact visual
dimensions), original-vs-fresh contact sheet and any pixel-level differences.
A passing two-second gate does not prove every possible duration, subject
or platform, so retain default CFG=1/eager as the conservative deployment
and expand real-GPU validation before enabling further algorithmic changes.

No Salad bootstrap/download changes are part of this work.
