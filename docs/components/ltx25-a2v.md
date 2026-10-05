# LTX 2.5 audio-to-video avatar mode

The LTX worker also exposes an audio-to-video mode for avatar segments.

## Contract

A2V generation receives:

- a prepared avatar/reference image;
- the audio segment for the target interval;
- a prompt describing the intended shot;
- the same model/runtime boundary used by the LTX worker.

The orchestration layer is responsible for deciding which narration intervals contain the avatar,
cutting the canonical audio with STT-derived timings and supplying the reference image for those
segments.

The worker does not infer timeline ownership itself. For professional `run`, an avatar image
supplied at the Qwen 1280x736 size is proportionally fitted to the 1920x1080 A2V output with
edge-extended side margins, rather than cropping its top and bottom. Other source aspect ratios
retain the previous centered crop, avoiding large repeated margins for portrait or wide
references. Internal padding to the 1920x1088 model grid is removed after decoding. A2V remains
a standalone worker capability selected by the B2 orchestration layer.

## Output

A2V returns a single video artifact through the normal inference envelope. Production geometry remains
aligned with the 16:9 LTX pipeline and downstream composition.

Request identity includes the inputs and generation-defining parameters so a repeated segment can use
the shared replay/idempotency path.

## Optimized professional `run` A2V

New professional `run` generations use the immutable
`LTX_A2V_RUN_OPTIMIZED_PROFILE`
(`ltx25-a2v-static-reference-distilled-fp8none-triton-progressive8-a95ab-run-v2`).
The generation recipe remains the accepted static-camera reference recipe:
the same negative prompt, stage-1 image strength 0.8, video CFG 1.25,
8+3 diffusion schedule, frozen audio conditioning, seed, prompt and fps.
The promoted execution path uses FP8 CAST, GPU-resident Stage 1, strict
Triton DiffVAE and CPU-progressive Stage 2 with block group size 8.

Normal `run` requests 1920x1080/24 output for both A2V and I2V so every generated
clip and the final compositor share one Full-HD geometry. The validated batch2 test
used a real GPU batch of two and therefore fused a true effective B4
conditional/unconditional transformer forward. A normal single A2V request
has an effective CFG batch of two; its configured forward cap remains 4 so
the worker does not introduce an artificial split, but it does not fabricate
a second video merely to claim B4.

Production parallelism is horizontal rather than forcing unrelated speech segments into one
GPU batch: the A2V group is capped at two RTX 5090 replicas and the autoscaler assigns one
active A2V job per replica. Two pending A2V clips therefore execute concurrently even when
their audio-driven frame counts differ. I2V follows the same two-replica/one-job policy in its
separate Salad group.

All new B2 A2V jobs use the run-v2 profile in their request identity and run
fingerprint. Historical run-v1 jobs remain immutable/replayable. Before an
MP4 is accepted, the sidecar must prove the run-v2 profile, FP8 CAST,
no-offload Stage 1, strict Triton, guidance cap 4, progressive group 8,
GPU Stage 1, progressive-CPU Stage 2 and the unchanged static-camera
generation parameters.

**Rollout gate:** `scripts/pipeline/run_b_pipeline.py` requires
`SALAD_LTX25_RUN_V2_IMAGE=<repository>@sha256:<digest>` and verifies that
the normal Salad A2V group is serving exactly that immutable image before a
new paid A2V submission. The digest cannot equal the historical
`SALAD_LTX25_RUN_STATIC_FP8_IMAGE` or
`SALAD_LTX25_PAGEABLE_A2V_IMAGE`. Source-code merge alone does not deploy
the worker; a new Docker image must be built, pinned and prepared separately.

**RTX 5090 run-v2 acceptance (2026-10-05):** the controlled Full-HD paired
trial used the exact Eager baseline inputs, independent seeds 5243/5244,
true GPU batch2, guidance forward batch 4, strict Triton and progressive
Stage2 group8. It completed both 1920x1080/24 outputs with 137 frames and
audio. Transformer inference was 192.54 s for the pair. Stage1 showed zero
process swap growth and no detected host pressure; the observed process
VmHWM was about 35.8 GB and peak allocated VRAM about 28.8 GB. Cgroup memory
counters were not exposed, so this does not prove the exact container-wide
peak; production therefore retains the validated 60-GiB host-memory class.

Against the approved Eager pair, sample A measured mean PSNR 40.05 dB /
SSIM 0.98415 and sample B 40.61 dB / 0.98446. Both audio-envelope lag
comparisons reported a 0.0-s delta. The operator accepted this performance
for promotion. The run-v2 implementation keeps the generation recipe fixed
and promotes only the validated execution backend choices.

## Validated fast reference production profile

`LTX_A2V_PRODUCTION_FAST_PROFILE` (`ltx25-a2v-reference-distilled-fp8none-eager-a95ab-prod-v1`)
is the default for **new direct A2V requests** and the smoke CLI's `--profile fast`.
It runs the unchanged distilled two-stage **reference** recipe using FP8 CAST,
no CPU weight offload and Eager DiffVAE. Its immutable identity is new:
existing production, static-camera and experimental job IDs remain valid and
are not silently replayed with different generation semantics.
The worker still claims the normal `video.ltx25.audio_to_video` queue;
`a2v_experimental` remains isolated.

RTX 5090 acceptance run on 2026-10-04: 3/3 successful experimental samples of
this exact no-offload reference recipe, 3/3 CPU-reference controls, decoded
audio and decoded video hashes matching across all six, approved human
lip-sync. For a ~5.71 s clip, observed no-offload warm inference averaged
~73.73 s and total generation ~101.55 s (two warm samples). GPU stability
beyond this six-generation run and startup improvements are not established.

Professional B2 `run` uses the promoted `LTX_A2V_RUN_OPTIMIZED_PROFILE`
with strict Triton and progressive Stage 2 group8. Historical static-safe profiles remain
available only for immutable recovery of previously submitted jobs; they are not selected for
new `run` requests.

To deploy new code to Salad, publish a new Docker digest from `main`, then
follow the stopped-group/lock checks before changing the production group
image. Merging this source profile does not update a running Salad image.

## Runtime

A2V uses the existing Salad service `ltx25` with `LTX_WORKER_TASK_MODE=a2v`.
I2V uses the separate Salad group `ltx25_i2v` with `LTX_WORKER_TASK_MODE=i2v`.
Both can use the same pinned model family and Docker image, but they never share
resident GPU containers or Postgres task ownership. The I2V infrastructure and an
opt-in experimental camera profile were integrated in PR #238 after a real
technical smoke. Camera stabilization remains visually incomplete: do not treat
the opt-in `static-camera` profile as a fixed-camera guarantee. Before enabling
the new capacity controller, prepare the A2V group with the task-restricted
image; its previously deployed image must not claim I2V jobs.

A2V keeps the shared request ceiling (`DEFAULT_GPU_MAX_ATTEMPTS=5`) for
non-memory failures, but CUDA OOM is limited to **two physical attempts**.
The upstream DiffVAE error `Cannot fit a DiffVAE decode tile under the memory
budget` is a deterministic budget failure, not a transient CUDA OOM: it
becomes terminal after **one** inference attempt. This prevents five
identical paid decoder-budget failures. Other CUDA-device or cuDNN/SDPA
failures retain the normal bounded retry policy. Input/contract failures
remain non-retryable.

The historical B2 v3 recovery selected immutable A2V `static-safe` **v3**.
It retains both prompts, 1280×720 output at 24 fps, the audio, seed,
sampler stages and FP8_CAST disk offload. It removes v2's fixed 2-GiB
decoder activation budget, which cannot cover upstream model cost, safety
reserve and full-volume stage-4 features for the failed `test02` clip.
The v3 decoder instead sizes tiles from the live GPU activation budget
after upstream cleanup, less an additional 1-GiB margin. This is still a
memory heuristic, not a guarantee that 720p A2V will finish on a 32-GB
GPU. v2 remains supported only for already submitted requests.

Because v3 has a new generation profile and fingerprint, `test02` must
use a **new Docker image** and a **new job ID**. The host runner requires
`SALAD_LTX25_AUTOTILING_A2V_IMAGE=<repository>@sha256:<digest>` in `config/salad.env` to match
the deployed A2V group. The old `SALAD_LTX25_SAFE_A2V_IMAGE` variable
cannot certify a v3 image. Before submitting new paid work, B2 checks all
previous incomplete runs: a failed job is recoverable only when Postgres
confirms terminal CUDA OOM or (for A2V only) the precise DiffVAE decoder
budget error. Other failed, cancelled, pending, running, or ambiguous jobs
block replacement. Historical manifests, job IDs and costs are preserved.

Do not rerun the full script or resubmit the failed v2 job. First verify
Postgres and idle Salad groups, stop the Capacity Controller, prepare the
new A2V image with a unique tag, pin its digest in `config/salad.env`, restart the
controller and run `.\\run.ps1 test02 --generate-video`. Offline tests
cover budget calculation, classification, retry cap and recovery; **v3 has
not been GPU validated**.

### Production v4: per-attempt persistent GPU diagnostics

The third paid `test02` recovery (v3) failed with CUDA OOM after two
attempts. Salad logs exported by job ID omitted the traceback and CUDA
phase data, so **the exact offending operation was then unknown**. The
diagnostic v4 attempt selected the same model, 1280×720/24fps, audio, prompts,
samplers, offload and live DiffVAE tiling budget as v3. Only its immutable
profile identity and observability differ; v2/v3 jobs remain replayable,
but the terminal v3 run must not be resubmitted.

The worker creates immutable JSON checkpoints for every claimed A2V attempt
in R2 at `diagnostics/ltx-a2v/<job_id>/attempt-XX/NNN-phase.json`.
Instrumentation marks model build, Gemma prompt encoding, audio VAE,
image conditioning, each diffusion stage, latent upsampling, tiling,
DiffVAE decode, output encoding and failure. Checkpoints include
timestamp, job/attempt/instance, GPU UUID/PCI where nvidia-smi supports
it, torch CUDA free/reserved/allocated/peak/num_ooms, process
RSS/pinned memory, cgroup memory usage and generation profile.
Synchronized checkpoints after major GPU boundaries surface delayed CUDA
errors nearer the responsible stage. No prompt, reference image, audio
samples or API secrets are included. The final failure JSON contains the
full Python exception chain and traceback (up to 64 KiB); Postgres
`last_error` includes the phase and R2 key.

A killed process can lose its final traceback but leaves previously
uploaded checkpoints. R2 failures must never replace the original GPU
exception; in that case Postgres retains the last known phase when Python
catches the error. This does not promise diagnosis if both the worker and
R2 fail before the first checkpoint.

The v4 runner requires an exact immutable deployed image digest in
`SALAD_LTX25_DIAGNOSTIC_A2V_IMAGE` in `config/salad.env`; v3's image setting is not accepted.
Publish a **new** Docker tag and prepare only the stopped `ltx25` group
after stopping the singleton Capacity Controller. Do not modify
`ltx25_i2v`. Restart the controller and recover the saved `test02`
with `.\\run.ps1 test02 --generate-video`. The subsequent real v4 run
failed at Gemma's pinned host buffer on A2V beat 7C.

To inspect a completed or failed attempt without Salad UI filtering:

```powershell
uv run --locked --extra dev python scripts/salad/inspect_ltx_a2v_diagnostics.py --job-id <new-A2V-job-ID>
```

The local command queries Postgres and downloads all matching R2 objects,
including the failure traceback if it was emitted. Default output:
`data/output/ltx-a2v-diagnostics/<job_id>/`.

### Production v5: safe pageable Gemma fallback and selective v4 recovery

Both paid v4 attempts for beat 7C failed while creating the upstream
`StreamingModelBuilder` CPU `BufferPool` for Gemma. Its
`cudaHostRegister` path returned no registered buffer; the second
`torch.empty(..., pin_memory=True)` path reported `AcceleratorError: CUDA
error: out of memory`, despite about 30 GiB of free VRAM and very little
PyTorch CUDA allocation. Beat duration is not implicated in this allocation.

The A2V Dockerfile patches **only** the fixed upstream LTX source revision
during image build; builds fail if the exact source anchors have drifted.
Upstream pinned-host registration and PyTorch pinned allocation remain the
first choices. If pinned allocation specifically fails with CUDA OOM, the
patched allocator allocates a pageable CPU buffer of the same byte size.
The patched `WeightsProvider` uses a **blocking** H2D copy when
`cpu_buffer.is_pinned()` is false, before the buffer may be released or
reused; pinned buffers retain asynchronous copies and existing event guards.
Unrelated exceptions are not suppressed. A real host-RAM limit remains a
possible terminal failure and pageable transfers may be slower.

Immutable v5 changes only this host-memory runtime and diagnostics: the
previous v4 generation configuration (model, 720p24, audio, prompts, seed,
samplers, FP8 disk offload, live tiling plus 1-GiB reserve) is retained.
The new diagnostic events include requested pinned buffer bytes and the
CUDA host-registration error code; Linux `RLIMIT_MEMLOCK` and available
cgroup v1/v2 counters are included where accessible.

The host runner requires `SALAD_LTX25_PAGEABLE_A2V_IMAGE` to equal the
exact new deployed `ltx25` image digest. Previous image flags remain
historical and do not permit new paid work. After Postgres confirms v4's
terminal failure and the capacity controller is stopped, publish and
prepare only `ltx25`, pin its digest in `config/salad.env`, restart the controller, then use
`.\\run.ps1 test02 --generate-video`. The recovery checks the **original v4 run's 20-character fingerprint**
(the exact saved manifest/directory format) and each completed job's
full immutable request fingerprint, media hash, ffprobe result and A2V
metadata before copying
those clips into the v5 run. Completed v4 I2V and A2V clips are never
resubmitted, and the old manifest is preserved as superseded. Missing
or incompatible completed outputs block paid regeneration instead of
silently charging twice. The failed beat receives a new v5 job ID.

The 12-second avatar-group ceiling already exists and is unchanged;
this fix does not segment speech or alter successful videos.
Offline tests cover the fallback, blocking copy and v4 reuse, but
**no paid v5 GPU success is claimed**.

## Speech-driven avatar settings

The existing **fast** and **dev** profiles remain available for comparison. They are not
treated as a functional rollback for avatar lip-sync.

Development adds an isolated **reference** profile whose purpose is to reproduce the
LTX-2.5 distilled two-stage recipe closely enough to establish a quality baseline. It
uses the shared distilled transformer, the upstream 8-step `DISTILLED_SIGMAS` schedule
and 3-step refine schedule, Stage 1 image strength 0.7 with the LTX-2.5 Euler ancestral
sampler (`eta=1`, `s_noise=1`, sampler-noise seed offset `+10000`), then Stage 2 image
strength 1.0 with plain Euler. Audio conditioning is frozen with zero noise in both
stages.

The reference profile intentionally keeps the RTX 5090 safety/memory regime already
validated by this worker: `FP8_CAST`, CPU offload and eager-SDPA DiffVAE decoding. This
is recorded in result metadata as a numerical/runtime deviation from a BF16 reference;
BF16 comparison is a later GPU experiment, not a prerequisite for the first functional
lip-sync baseline.

### Experimental static-reference camera controls (restored from PR #235)

`static-reference` is an explicitly selected **experimental A2V** profile restored
from the original PR #235 implementation. It does not change the default
`reference` recipe, the production route, I2V or the positive user prompt.
Only Stage 1 adds a camera-only negative prompt (zoom / dolly / drift /
reframing), increases reference-image strength from 0.7 to 0.8 and applies
video CFG 1.25 with upstream `GuidedDenoiser`; Stage 2 remains the original
3-step `SimpleDenoiser` with image strength 1.0. Stage 1 keeps the distilled
8-step Euler-ancestral sampler and frozen speech conditioning. The additional
CFG path can increase GPU work; no speed or camera-stability improvement is
claimed without a real visual comparison. Ordinary `reference` remains
unchanged and continues to use `SimpleDenoiser` without negative CFG.

Use the controlled A2V smoke with `-Profile static-reference` and a newly
published, immutable worker image supplied by `-ExpectedPinnedImage`. This
recovery does **not** include the abandoned two-GPU race or alter the Salad
manifest or Docker tag. A current deployed image will not acquire this Python
code merely because the local branch was updated. Keep the group stopped
until the exact new image is built and pinned. Compare the generated MP4 with
the same original image, audio, seed and positive prompt, and inspect camera
framing, mouth movement, identity and final speech. No paid GPU test or visual
acceptance is implied by restoring the code.

### Experimental guided talking-avatar profile

`guided` is an **opt-in, non-production** comparison profile. It invokes the pinned
upstream `A2VidPipelineTwoStage` with the full/dev transformer, 30-step Stage 1
Euler sampling, the distilled LoRA at strength 0.8 only in Stage 2, and the
video guidance values from the LTX talking-avatar blog (CFG 3.0, STG 1.0,
rescale 0.7, A2V modality 3.0, STG blocks `[29]`). These values intentionally
override the pinned LTX-2.5 checkpoint's STG block `[28]` only in `guided`.
Audio stays frozen in both stages. The upstream `A2VidPipelineTwoStage` API
exposes only the video guider and constructs its audio guider internally with
default parameters, so the blog's audio CFG 7.0 is **not** applied or claimed.
The existing `dev` profile deliberately keeps its old disabled-guidance contract;
`reference` and `fast` are unchanged. For guided tests only, the worker reuses
reference audio decoding, upward `8k+1` grid snapping and silence padding,
then encodes the conditioning waveform as lossless FLAC before passing it to
upstream; `monje.wav` remains a valid **input to Bordell**, not the file passed
to the model. The worker passes an explicit frame count to avoid truncating the
last words.
The upstream guided stages retain image-conditioning strength 1.0; they do not
claim to reproduce the distilled ComfyUI recipe or its resolution.

**No guided GPU or visual validation is implied by this code.** The currently
pinned Salad image v10 and manifest use `LTX_INCLUDE_A2V_DEV_ASSETS=false`; a
guided smoke must not allocate GPU until a newly versioned image containing
this code is built, published, checked by digest and pinned to the stopped
group, and the manifest explicitly enables the optional dev checkpoint and
Stage 2 LoRA. When dev assets are enabled, A2V preparation must wait for
both optional files and the completed, atomic installed-model manifest before
Postgres job polling begins: the shared distilled files becoming available is
not sufficient. The guided smoke allows a three-hour **pending** window for cold
bootstrap (the existing 30-minute default remains for other profiles); an
explicit `--pending-timeout-seconds` overrides it. Reconcile the existing group
via the protected Capacity Controller lifecycle; do not replace the group, reuse
a Docker tag, or change production A2V routing. The PowerShell wrapper fails before allocation while the manifest
disables dev assets. Verify model-cache provenance and free storage before the
first download. Do not run an existing reference benchmark concurrently.

Inspect the mouth against speech at bilabial consonants, vowel openings,
pauses and phrase boundaries, and compare against the fixed `reference` baseline
using the same avatar, input WAV, prompt and seed but a fresh segment ID. A valid
MP4, preserved audio and green CI alone are not lipsync acceptance.

### Guided Gemma pinned-memory failure (September 26, 2026)

The RTX 5090 `guided` smoke failed before denoising at 40 GiB **and** 60 GiB
Salad container RAM. The pinned upstream `OffloadMode.CPU` path tried to
allocate a large page-locked **host** buffer while building the Gemma text
encoder (`StreamingModelBuilder._build_pinned_source`), then PyTorch raised
`torch.AcceleratorError: CUDA error: out of memory`. Its immediately preceding
VRAM snapshot reported about 32.4 GB free: do not diagnose this traceback as
22B diffusion running out of GPU VRAM, or assume additional container RAM fixes
pinned-memory registration. The failed jobs did not produce a guided video.

The `guided` profile now uses upstream `OffloadMode.DISK` for the two-stage
pipeline (including Gemma), bounding pinned CPU staging slots rather than
pinning all transformer blocks. Existing `fast`, `reference`, and `dev` keep
`OffloadMode.CPU`. This is an official upstream streaming mode, not an
upstream monkey-patch; its repeated disk reads can increase generation time.
The guided profile version and returned `offload_mode` metadata identify this
change. Keep the 40 GiB default in the tracked Salad manifest; a local 60 GiB
comparison is not evidence that 60 GiB is required. This is a code-level
mitigation awaiting real RTX 5090 and visual lip-sync validation, not a claim
that the guided benchmark passed. Publish a **new immutable image** and use a
fresh segment ID; the old worker image still contains the CPU-pinned path.

### Real guided DISK smoke (September 26, 2026)

The controlled RTX 5090 monk smoke with the pinned `fp8disk-eagersdpa-v2`
profile **completed** through PostgreSQL/R2 and left the Salad group stopped.
Job `ltx-a2v-monje-guided-disk-20260926-015706-4d32907058ca`
used seed 4242 and the five-second mono 24 kHz WAV. The resulting MP4
contained 121 frames at 1280x720 / 24 fps, with stereo AAC and
5.042 s output duration. `offload_mode=disk`; inference took 530.005 s,
total elapsed was 546.586 s, and peak CUDA allocation was 17,181,615,616
bytes. The reported `pipeline_reused=true` means this is not a cold-model
timing. Video SHA-256:
`80ef9a18e504d0f79ad0b5c4f5a70444b4109648c19ecc98f75ecd74de71bfd4`.

**Functional transport and memory-path success is not visual acceptance.**
The last frames deform visibly near the end of the clip; frame-by-frame
phonetic lip-sync and identity stability have not passed acceptance.
The guided profile remains opt-in/experimental, outside production
routing, and is not suitable for performance or quality guarantees.
Do not drop the final voiced audio to hide the visual defect. A previous
GPU attempt also lost its instance after Stage 1; the successful later
smoke does not establish the cause of that interruption.

### Temporal contract

Reference A2V never snaps speech down to the previous `8k+1` frame. The worker first
decodes speech to deterministic stereo PCM, measures the decoded sample count, selects
the first valid `8k+1` video grid that covers all speech samples, and pads only the
conditioning waveform with the silence needed to cover that grid. The Audio VAE output
must contain enough latent positions for the selected video duration; latent tensors are
not padded synthetically.

The reference profile currently accepts at most 12 seconds of effective decoded speech.
A longer narration must be segmented by orchestration rather than cut arbitrarily inside
the worker.

Profile is part of job identity, so reference outputs cannot replay legacy fast/dev R2
artifacts.

## Validation

Targeted real validation uses:

```text
scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1
```

The controlled PowerShell smoke and direct submit CLI default to `reference` for
functional avatar validation. `fast` and `dev` remain available only when explicitly
selected for comparative benchmarks; their passing MP4/audio checks do not qualify lip-sync. Reference
validation verifies the selected checkpoint family, both stage schedules, sampler
semantics, per-stage image strengths, frozen audio, upward temporal-grid padding and the
decoded voiced span at the end of the generated MP4.
For a five-second audio clip, pass `-MaxGenerationSeconds 120` to fail validation if
`total_elapsed_seconds` exceeds the two-minute acceptance target. This measures the worker's generation, not Postgres pending time or cold model downloads.

The smoke accepts an explicit prompt, seed and output directory for matched comparisons
(e.g. the same avatar image and speech WAV). Run a single `reference` job and inspect its
mouth movement before scheduling a multi-run performance benchmark. Do not run the
controlled smoke concurrently with another LTX benchmark: both own explicit Salad capacity
and their cleanup stops the same group.

The smoke verifies technical and speech-preservation contracts, but a passing result does
not establish lip-sync quality. The reference profile is not considered operational until
a real RTX 5090 run demonstrates repeatable mouth synchronization and identity preservation
under visual review. The smoke does not replace production orchestration.
Transport IDs, timings and artifact hashes from individual validation runs belong in Git history.


## Historical readiness regression

A September 24, 2026 warm run exposed a real worker bug: the readiness path attempted to acquire the
same long-running LTX mode lock held during A2V inference. Repeated probe timeouts could therefore
make a healthy instance appear not ready while the GPU was still generating.

The worker now keeps the shared inference lock for model construction/generation while readiness uses
prepared bindings and read-only validation without taking that lock. R2 and database readiness checks
remain active.

The current controlled smoke uses explicit Salad capacity and the Postgres job transport. It does not
depend on provider-queue dispatch, queue autoscaling or transport IDs.

The readiness fix first shipped in
`ltx25-a2v-torch211-cu128-eagersdpa-xet-fast-v9`. The active worker image is defined by
`deploy/salad/services.json`; after changing worker or claim code, build a new versioned image
and verify its immutable digest before updating the stopped Salad group. A passing MP4/audio
contract still does not prove
lip-sync quality; visually inspect mouth movement against the supplied speech before accepting an A2V
profile.
