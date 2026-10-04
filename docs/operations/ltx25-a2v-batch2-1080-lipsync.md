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
