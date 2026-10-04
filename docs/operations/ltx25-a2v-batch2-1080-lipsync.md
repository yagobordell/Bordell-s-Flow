# Isolated A2V true GPU batch 2 at 1920x1080

This branch is **NOT production-approved** and must not overwrite any
production A2V profile, I2V PR or existing Salad group. Only the separate
`ltx25_a2v_experimental` group registers
`video.ltx25.audio_to_video_batch2_experimental` when
`LTX_EXPERIMENTAL_A2V_BATCH2_ENABLED=true`. All four inputs and both
outputs are part of one atomic Postgres/R2 job. No implicit sequential
fallback is allowed.

## Pinned generation behavior and lipsync

Two independent image, prompt, speech-waveform and RNG streams are
individually conditioned, then stacked ONLY for genuine GPU batch-2
transformer forwards. Preserve the reviewed static A2V fast recipe exactly:
positive prompt per clip, approved static negative prompt, CFG 1.25,
stage 1 0.8 image strength, ancestral 8-step sampler (seed + 10000
for its own per-sample stochastic stream), stage 2 1.0 image strength,
Euler 3-step sampler, FP8 cast, eager tiled SDPA VAE (do not enable
crash-prone NATTEN or switch to unverified Triton). Both stages freeze
each clip's own encoded speech. Original independently decoded/padded
PCM—not transformer-generated audio—is muxed into its *own* output.

Audio duration owns the temporal grid: use the reference padding routine
for EACH audio first. The free client and server reject unequal 8k+1
frame counts BEFORE transformer work; two audios need not have identical
byte counts or exact durations, but MUST snap to the same frame count.
The initial experiment supports only 1920x1080, 24fps. Internally the
LTX grid is padded to 1920x1088, and each output is cropped individually
to exact 1920x1080. Input avatar PNGs may have their original dimensions
and retain the existing centered reference-avatar preparation.

For GPU viability, stage 1 is the approved FP8 no-offload path; stage 2
uses the SAME weights and FP8 policy via CPU block streaming. The
previously audited exact pinned upstream progressive loader patch is
reused for A2V exclusively when the NEW alternate stage-2 wrapper opts
into it. Its upstream default is OFF, so production A2V and unrelated
I2V jobs retain their existing model loaders. Video VAE decoding and
muxing occur separately per video with one shared unchanged VAE model
lifetime. No sound waveforms, temporal decoder states, tiling or random
generators are pooled between outputs.

The dedicated experimental Salad group is provisioned with 65536 MB
RAM on the RTX 5090. This is a deployment budget, **not** proof that
the batch will fit VRAM/RAM. Do not hide an OOM by generating sequential
B1 clips and calling it a batch-2 success.

## Free checks and exact Docker

From the *new A2V branch*, first confirm the isolated CI and the full
PR CI are green; build and push a NEW Docker, extract its immutable digest,
then run the strict offline check:

```powershell
git fetch origin
git switch feature/ltx25-a2v-b2-1080-lipsync
git pull --ff-only origin feature/ltx25-a2v-b2-1080-lipsync
uv sync --locked --extra dev --python 3.12

$Tag = "docker.io/yagobordell/ai-video-factory:a2v-b2-$(Get-Date -Format yyyyMMddHHmmss)"
$Meta = Join-Path $env:TEMP "ltx25-a2v-b2-image.json"
docker buildx build --platform linux/amd64 -f docker/workers/ltx25/Dockerfile -t $Tag --metadata-file $Meta --push .
if ($LASTEXITCODE -ne 0) { throw "A2V Docker build/push failed" }
$Digest = (Get-Content $Meta -Raw | ConvertFrom-Json).'containerimage.digest'
if ($Digest -notmatch '^sha256:[a-fA-F0-9]{64}$') { throw "No image digest" }
$Pinned = "docker.io/yagobordell/ai-video-factory@$Digest"
uv run --no-sync python scripts/smoke/verify_ltx25_a2v_batch2_image.py --image $Pinned
if ($LASTEXITCODE -ne 0) { throw "A2V Docker ABI preflight failed" }
```

`LTX25_A2V_B2_DOCKER_ABI_OK` means the exact image imports its
pairing/frozen-audio path, honors the pinned upstream ancestral sampler,
and contains the progressive loader. It does not establish runtime
GPU stability or video quality.

## Local input verification and isolated Salad runtime

Supply exactly two lossless avatar PNGs, two voice audio files, prompts
and **different seeds**. Choose speech clips that snap to the same 8k+1
frame count; using equal-length 5-second clips for the first controlled
trial is recommended but not required. Set `$Trial` as follows:

```powershell
$Trial = @{
 ImageA = "C:\\PATH\\avatar-a.png"
 AudioA = "C:\\PATH\\speech-a.wav"
 PromptA = "A static-camera close-up of the avatar speaking naturally."
 ImageB = "C:\\PATH\\avatar-b.png"
 AudioB = "C:\\PATH\\speech-b.wav"
 PromptB = "A static-camera close-up of the avatar speaking naturally."
 SeedA = 5243
 SeedB = 5244
 RunId = "a2v-b2-$(Get-Date -Format yyyyMMddHHmmss)"
 ExpectedPinnedImage = $Pinned
 EnvFile = ".env"
}
.\\scripts\\smoke\\run_ltx25_a2v_batch2_trial.ps1 @Trial -CheckInputsOnly
```

This checks both real audio grids and the exact Docker WITHOUT starting
Salad. Correct any error before provisioning paid compute.

Only after confirming the experimental group is stopped, its controller
lock is available, and no other operator is using it, prepare the
dedicated group with the exact pinned digest:

```powershell
.\\scripts\\salad\\manage_salad_worker.ps1 -Action Status -Service ltx25_a2v_experimental -EnvFile .env -NonInteractive
.\\scripts\\salad\\manage_salad_worker.ps1 -Action Prepare -Service ltx25_a2v_experimental -SkipBuild -PinnedImage $Pinned -EnvFile .env -NonInteractive
.\\scripts\\salad\\manage_salad_worker.ps1 -Action Status -Service ltx25_a2v_experimental -EnvFile .env -NonInteractive
```

If (and only if) the group is **stopped, pending=False and configured
with the pinned digest**, the operator can explicitly start a *billed*
run using the SAME parameters:

```powershell
.\\scripts\\smoke\\run_ltx25_a2v_batch2_trial.ps1 @Trial -AllowPaidTrial -NonInteractive
```

The wrapper checks source imports, R2 and CPU media grids; performs a
fresh immutable-image ABI probe; refuses existing/stale/active Salad
groups; starts ONE experimental RTX 5090 instance and waits for two
/ready passes on that exact image; submits exactly one non-replayable
four-input atomic job with `max_attempts=1`; verifies two videos AND
both speech tails; and stops the group on success or failure. **Never
use the controller-override switch** to bypass another operator.

## Required acceptance

On a real GPU execution, require `mode=batch2`,
`actual_gpu_batch_size=2`, successful stage1 and stage2, no CUDA OOM,
no unexpected cgroup pressure, correct 1920x1080 24fps output and exact
per-clip grid frame counts. Both output MP4s must decode fully and
contain one video stream and their own audio stream. The automated
20-ms speech-energy timing and voice-tail checks protect against
muted/truncated/shifted audio, but CANNOT prove lip movements match
speech. Obtain B1 reference videos for **each exact matching**
avatar, audio, prompt, seed, 1080p geometry, frame count and static
guidance. Optional `ReferenceA/ReferenceB` entries in `$Trial`
automatically create `b1-comparison.json` (full-frame PSNR/SSIM
and relative audio envelope lag) beside the run's report. If input
provenance differs, do not interpret that comparison as a quality test.
Review the mouth movements through the entire 5-second clips,
including the last words and fast phonemes, side by side with B1.

CI unit tests and Docker ABI cannot establish real 32-GiB GPU headroom
or lip synchronization. Do not promote to main or change production
A2V's recipe until the operator signs off on actual output and
the measured times/VRAM and Salad worker RAM logs.


## Quality-preserving latency experiments

The accepted Eager/split-B2 pair remains the immutable quality baseline. Three
latency optimizations are isolated from the generation recipe:

1. **Identical prompt reuse inside one pair.** If and only if both literal
   positive prompts match, the pair runs the deterministic prompt encoder once
   and reuses the resulting positive/negative context tensors for the second
   slot. Seeds, images and audio remain independent. Differently worded prompts
   still execute two complete prompt encodes.
2. **Triton DiffVAE is opt-in only.** `VaeBackend=triton_experimental`
   requires the exact original Eager pair as references, runs the CUDA kernel
   probe in an isolated child process, forbids silent fallback and writes a
   full decoded-frame comparison for human approval.
3. **Fused guidance forward is opt-in only.** Stage 1 CFG naturally constructs
   an effective transformer batch of four: conditioned B=2 plus unconditional
   B=2. The accepted baseline uses `BatchSplitAdapter(max_batch_size=2)`,
   which serializes that effective B=4 into two B=2 forwards. Setting
   `GuidanceForwardBatchSize=4` leaves the upstream CFG tensors, ordering,
   sigmas, samplers, image strengths, audio latents and RNG streams unchanged;
   it only executes that already-constructed B=4 transformer input in one
   forward. Stage 2 remains logical B=2 and therefore has no arithmetic change.
   This may OOM on the RTX 5090 and has no sequential fallback.

The pinned upstream CPU block-streaming implementation already overlaps pinned
H2D copies with transformer compute on a dedicated CUDA stream. No additional
custom Stage-2 prefetch layer is added: duplicating that machinery would add
risk without a demonstrated quality-preserving benefit. Instead the worker
reports Stage-2 wrapper-build and denoise timings plus process/cgroup memory so
future work can target a measured bottleneck.

The optimized forward or Triton paths MUST receive the exact accepted baseline
`batch2-a.mp4` and `batch2-b.mp4` from one run, together with its original
JSON manifests. The smoke client rejects mismatched image/audio hashes,
prompts, seeds, geometry, frame count, profile, non-Eager decoder or a baseline
that itself used fused B4. Successful candidates still remain
`HUMAN_*_VISUAL_LIPSYNC_APPROVAL_REQUIRED`; PSNR/SSIM and audio-envelope
alignment are evidence, not automatic quality approval.

Example using the accepted baseline:

```powershell
$Trial.ReferenceA = "data/output/deployment-validation/ltx25-a2v-batch2/a2v-b2-20261004190627/batch2-a.mp4"
$Trial.ReferenceB = "data/output/deployment-validation/ltx25-a2v-batch2/a2v-b2-20261004190627/batch2-b.mp4"
$Trial.RunId = "a2v-b2-fused4-$(Get-Date -Format yyyyMMddHHmmss)"
.\scripts\smoke\run_ltx25_a2v_batch2_trial.ps1 @Trial \
    -GuidanceForwardBatchSize 4 -CheckInputsOnly
```

For the decoder-only comparison, retain split guidance and select Triton:

```powershell
$Trial.RunId = "a2v-b2-triton-$(Get-Date -Format yyyyMMddHHmmss)"
.\scripts\smoke\run_ltx25_a2v_batch2_trial.ps1 @Trial \
    -GuidanceForwardBatchSize 2 -VaeBackend triton_experimental -CheckInputsOnly
```

Do not combine fused-B4 and Triton in the first paid retest. Measure them
independently against the same accepted Eager/split-B2 pair so any timing or
quality difference has one cause.


## Experimental sub-200-second research gate (not yet hardware validated)

The accepted original Eager/split-B2 Full HD reference took 298.14 seconds;
fused B4 with Eager took 267.73 seconds. Strict Triton with split B2 took
299.17 seconds on a markedly paging-constrained host, despite reducing the
combined two-clip VAE+MP4 phase from 89.73 to 52.05 seconds. Do not add
these savings arithmetically or claim that combining both reaches 200 seconds.

The next **isolated** experiment combines the already-tested B4 forward and
strict Triton decoder. It changes no A2V model weights, prompts, audio freeze,
CFG equation, sampling sigmas, seed order, frame count or tiling policy. The
new `-MaxStage1MajorPageFaults 20000` test-only placement guard measures
*incremental process-level* major page faults from pipeline acquisition
through Stage 1. If that increment exceeds 20,000 (original B4's cumulative
count was about 1,600; the unusually slow split-B2 Triton run exceeded
170,000 by Stage 1), abort **before** expensive Stage 2 and VAE. Missing
or non-monotonic proc counters also fail closed. This is not proof that a
remaining host is swap-free: cgroup memory can be hidden by Salad.

A2V now forces cyclic garbage collection at two natural lifetime
boundaries: after upsampling and dropping stage1 tensors before loading
the pinned Stage 2, and after Stage 2 has fully exited its streaming
context and before lazily building the shared VAE. Those changes touch
only already-dead Python allocations. Reports include both GC costs and
memory snapshots so we can reject them if collection is slower than any
memory benefit. Do not introduce extra CUDA synchronization.

The optional `-PerformanceTargetSeconds 200` records an explicit
`MET`/`MISSED` comparison using **total pair elapsed seconds**. A miss
does not discard paid videos or invent a speedup; all 137-frame PSNR/SSIM
and independent audio-envelope comparisons still run against the original
immutable Eager/split-B2 pair. A successful target result is not quality
approval: operator still verifies both lipsync and facial motion.

The experiment MUST run on a freshly built immutable Docker digest
including the new host-guard ABI. It must run the `-CheckInputsOnly`
free preflight (including strict Triton ABI) and only prepare the
**stopped, operator-owned experimental** A2V group. Do not reuse the older
`efb47d0...` image; it cannot honor the new guard. The paid wrapper
refuses a nonmatching remote digest, active instance, or pending change
and stops only the capacity it started.

Next phase *if the measured combined run remains >=200 seconds*: inspect
`stage1_denoise_seconds`, `stage2_denoise_seconds`,
`stage1_release_gc_seconds`, `stage2_release_gc_seconds`,
`memory_before_vae`, and decoder/MP4 times. Do not change sampling
steps/CFG/audio or introduce approximate sparse attention solely to
force the 200-second metric. As a separately priced scheduling
alternative, concurrent independent B1 jobs on separate GPUs could
shorten wall time, but are **not** the same one-GPU B2 benchmark and
must include both running-GPU cost and cold-start cost.


## Cold-checkpoint bottleneck postmortem and grouped-stage2 candidate (2026-10-05)

The full Salad CSV for the 628.34-second B4+Triton run shows **two
different worker instances**: the first failed the 100-Mbps network probe
(61-71 Mbps) and requested replacement. The second downloaded about **71 GB**
of A2V assets between 21:47 and 22:10 and finished bootstrap at 22:14,
then accepted the job at 22:15. It took **56.08 s for Gemma and embeddings**,
**188.36 s to build the GPU-resident Stage 1 transformer**, **166.14 s to
build Stage 2**, but only **57.05 + 62.79 s to denoise** and
**30.26 + 24.36 s to encode both clips**. Stage 1 recorded only 1292
additional major page faults, so the previous page-fault protection
could not catch this kind of cold/slow checkpoint access. At most
39.83 GiB process VmHWM and 59.54 MiB process swap were reported;
the container memory controller was unavailable. The log strongly
implicates a recently downloaded, cold model filesystem, but existing
data cannot separate I/O throughput from CPU conversion during model
construction. Do not claim that the disk is the sole proven cause.

This experiment adds three *opt-in and strictly isolated* changes:

- `-MinCheckpointDirectReadMiBps 350`: before constructing Gemma or
  running the Triton GPU probe, perform eight distant 16-MiB aligned
  O_DIRECT reads on the original **42-GB transformer checkpoint**.
  Report median/minimum throughput. Reject slow or unsupported direct
  reads rather than mistaking cached reads for fast storage. The threshold
  is an initial experimental admission floor, NOT a guaranteed latency
  predictor. A failed gate still incurs any preceding Salad bootstrap.
- `-ProgressiveBlockGroupSize 8`: Stage 2 requests the SAME FP8 CPU-pinned
  destination buffers and SAME upstream per-key state-dict transforms,
  but batches up to eight consecutive transformer blocks into one
  restricted checkpoint read rather than issuing a new loader invocation
  for every block. Validate all expected keys and copy original bytes
  into the same final views, releasing raw tensors after every group.
  This reduces repeated metadata/setup calls and may improve locality
  without changing the model or forward equations. Additional transient
  raw weights (up to eight blocks) are expected, so reject OOM and
  monitor RAM/swap. I2V and the old A2V B2 default remain group=1.
- `-MaxStage1BuildSeconds 70`: abort safely after Stage 1 model build
  if the selected instance again takes an abnormal time, before running
  any denoise/Stage 2/VAE. Model-build log now reports process CPU
  seconds, process physical read-byte delta and major-fault delta
  independently for **each** stage, including failed slow builds.

The candidate retains fused-B4 CFG, strict Triton DiffVAE, 1080p,
137 frames, identical original input hashes, seed pair, FP8 policy,
sigmas, audio freeze and immutable original Eager reference comparison.
Nothing is promoted automatically. The comparison must be checked
visually for both facial motion and lipsync even when PSNR/SSIM and
audio-envelope alignment look good.

A new Docker digest is required. The exact-image free ABI gate rejects
an older worker without the grouped-loader source marker, new typed
parameters or O_DIRECT probe. After build/publish, use the existing
experimental PowerShell trial with:

```powershell
$Trial.RunId = "a2v-b4-triton-group8-$(Get-Date -Format yyyyMMddHHmmss)"
.\scripts\smoke\run_ltx25_a2v_batch2_trial.ps1 @Trial \
    -GuidanceForwardBatchSize 4 -VaeBackend triton_experimental \
    -ProgressiveBlockGroupSize 8 -MinCheckpointDirectReadMiBps 350 \
    -MaxStage1BuildSeconds 70 -MaxStage1MajorPageFaults 20000 \
    -PerformanceTargetSeconds 200 -CheckInputsOnly
```

Only after local preflight, immutable image ABI check, and stopped,
operator-owned experimental Salad group + exact-image verification
may the same command be executed with `-AllowPaidTrial -NonInteractive`.
Never force cleanup of a foreign execution.

**Important:** Even the previous fastest Eager-B4 run was 267.73 s.
Grouped Stage 2 and host admission are potential improvements, but
neither guarantees a sub-200-second result. Measure all phases and
the run's end-to-end elapsed time before claiming profitability or
merging PR #306.
