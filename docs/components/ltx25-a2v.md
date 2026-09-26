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

The worker does not infer timeline ownership itself. An avatar image supplied at the Qwen
1280x736 size is proportionally fitted to the existing 1280x720 A2V output with edge-extended
side margins, rather than cropping its top and bottom. Other source aspect ratios retain
the previous centered crop, avoiding large repeated margins for portrait or wide references.
Internal padding to the 1280x768 model grid is removed after decoding. A2V is still a
standalone worker capability, not a production stage.

## Output

A2V returns a single video artifact through the normal inference envelope. Production geometry remains
aligned with the 16:9 LTX pipeline and downstream composition.

Request identity includes the inputs and generation-defining parameters so a repeated segment can use
the shared replay/idempotency path.

## Runtime

A2V and normal image-to-video share the same LTX service and resident model family. They do not require
separate Salad groups.

A2V uses the shared GPU retry budget (`DEFAULT_GPU_MAX_ATTEMPTS=5`). OOM, CUDA-device and
cuDNN/SDPA failures remain retryable through the Postgres job state machine. Before a later
attempt, the A2V runner invalidates its resident pipeline so the next worker claim rebuilds clean
GPU state instead of reusing a potentially damaged model instance. Input/contract failures remain
non-retryable.

The worker-specific deployment contract is documented in [LTX 2.5 worker](ltx25-worker.md).

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

### Experimental compiled reference profile (latency A/B)

The opt-in `reference-compiled` profile uses the **same** distilled checkpoint,
8+3 sigma schedules, image strengths, Euler ancestral/standard samplers, frozen
padded speech, FP8_CAST weights, CPU offload and eager-SDPA DiffVAE as
`reference`. The only pipeline change is upstream
`CompilationConfig()`, which enables per-transformer-block `torch.compile`
with the pinned upstream default Inductor backend. This is a distinct generation
profile and resident-pipeline cache key; it does not silently modify `reference`,
`guided`, production routing or the Salad image manifest. Result metadata
reports `transformer_compilation=blocks` (versus `eager`).

Official pinned LTX optimization reference:
https://github.com/Lightricks/LTX-2/blob/a95ab856bf29407b6b066ede0abe1846050db56c/packages/ltx-pipelines/docs/optimization.md
The official `CompilationConfig` API is defined at the same pinned revision.
Compilation is opt-in upstream: an initial run may take **longer** due to graph
compilation. No speedup or bitwise-equivalent video is assumed, and compiled
outputs remain experimental until real GPU visual acceptance. Published community
RTX 5090 LTX benchmarks also use different model variants, shapes and video-only
workflows; do not transfer their timings to this A2V profile.

**No new image has been built or deployed by this branch.** The currently tracked
Salad image v10 cannot run `reference-compiled`. Before any paid smoke, build a
new immutable image from the reviewed branch SHA, verify its digest and source
revision, update the stopped existing LTX group under Capacity Controller lock,
then use a new, uncached segment ID. Do not run a smoke against the old image or
start another LTX benchmark concurrently.

For the A/B benchmark, use the same monje WAV/avatar, prompt, seed 4242,
1280×720 at 24 fps and 5-second voiced input. Measure separately: cold worker
bootstrap/model load, first compiled inference, and **at least three** subsequent
jobs on the same RTX 5090 worker, with identical settings and distinct segment
IDs. Compare the warm median and end-to-end elapsed time against the historical
`reference` 157.360 s inference / 175.876 s total and a fresh eager reference
run on the same pinned image. Require matching speech-sample counts, technical
MP4 checks and manual frame/phoneme/identity review, especially at the final
frames. Do not merge or promote the compiled variant on timings alone.

Other official candidates are **not enabled**: `fp8-scaled-mm` changes numerical
arithmetic and requires an independent visual A/B; `combined_compile` DiffVAE
has higher VRAM use and needs a measured decode bottleneck and a memory-safe
GPU smoke first. The full `guided` profile currently needs DISK offload because
upstream CPU-pinned Gemma construction failed even with 60 GiB container RAM.
Simply switching its offload mode back to CPU is not an optimization.

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

### Controlled Salad bootstrap readiness (cold-start smoke)

The controlled `run_ltx25_a2v_smoke_controlled.ps1` smoke now waits for **one
current-version LTX instance to pass the configured HTTP `/ready` probe twice**
before submitting the A2V job to Postgres. The group-level `running` status
only establishes capacity; it does **not** establish that multi-gigabyte model
downloads and the worker's model validation have finished.

Readiness polling uses Salad's read-only container-group and instance API.
It requires an immutable deployed image, matching image repository and current
group version, an unchanged single-replica group, the configured `/ready`
probe, and consecutive `started=true, ready=true` observations of the same
instance. Pass `-ExpectedPinnedImage repo@sha256:digest` to also validate
the exact approved image. Unready or replaced instances reset the streak.
Authentication/configuration errors fail immediately; transient API errors
are retried a bounded number of times.

Bootstrap has a **separate, explicit** 7200-second default budget, configurable
via `-BootstrapTimeoutSeconds` (120–21600). A bootstrap timeout fails before
an inference job is enqueued and the wrapper's `finally` still stops LTX.
The existing 1800-second pending budget starts only after the worker is ready
and the Postgres job is submitted. The running-job budget and worker's
download watchdog remain unchanged. The helper only changes local scripts:
the already built, reviewed LTX image can be reused without another Docker
build or Salad `Prepare`, provided its deployed immutable digest is verified.

Example after the stopped LTX group is prepared with the approved image:

```powershell
$SegmentId = "monje-compiled-ready-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File .\scripts\smoke\run_ltx25_a2v_smoke_controlled.ps1 `
    -Audio ".\data\input\avatar\monje.wav" `
    -AvatarImage ".\data\input\avatar\monje.png" `
    -Profile reference-compiled `
    -Seed 4242 `
    -SegmentId $SegmentId `
    -ExpectedPinnedImage $PinnedImage `
    -BootstrapTimeoutSeconds 7200 `
    -EnvFile .env -NonInteractive
```

The initial compilation may still be slow. A successful readiness wait proves
the model bootstrap completed, not that lip-sync or GPU inference is accepted.

### Instance-aware Salad startup and bounded node reallocation

The controlled A2V smoke no longer waits up to 30 minutes for the *entire
container group* to report `running` before it can check readiness.
Its opt-in `-AllowBootstrappingInstance` path in
`manage_salad_worker.ps1 -Action Start` waits for an instance of the current
group version to reach `state=running, started=true`, even if the group
still reports `deploying`. The separate readiness helper accepts a
current-version instance reporting `ready=true` twice while the group
is running or deploying. The API instance identifier is `id`, not
`instance_id`. This corrects the first-readiness implementation without
changing ordinary Start behavior for other workloads or production.

The group still selects the **RTX 5090 (32 GB) class**, not a particular
physical GPU. The Salad public API cannot rank available nodes by model
performance or guarantee that the next assigned machine is better.
Before the LTX container has started, the Start manager watches official
per-instance `pulling_progress`; after **480 seconds without image-pull
progress**, it can request an individual Salad reallocation, at most
**twice per controlled start**, under the same Postgres advisory lock as
the Capacity Controller. It rechecks group version, desired replicas,
pending-change state, instance ID, pull state and progress immediately
before requesting reallocation. Image pulls with measurable progress are
retained.

Once the container has started, the host stops making decisions based on
the lack of readiness. The already deployed LTX worker runs a 100 Mbps
Hugging Face network preflight and an 8 MiB/s sustained download watchdog
(with configured grace and window), which request node reallocation only
for measured poor network/download behavior. This avoids throwing away
a healthy, actively downloading model just because the multi-gigabyte
bootstrap takes time.

The host startup is still bounded to 30 minutes and reports the current
instance state/pull progress and any reallocation requests; after the
container starts the separate 7200-second worker readiness budget
applies. No job is enqueued until readiness succeeds, and the
controlled script always stops Salad in `finally` on success or error.
This fix changes only local scripts/tests/docs, **not the worker image
or weights**, so the existing compiled image can be reused after
confirming its immutable digest and stopped group state.

Official Salad API references:
- https://docs.salad.com/reference/saladcloud-api/container-groups/list-container-group-instances
- https://docs.salad.com/reference/saladcloud-api/container-groups/reallocate-container-group-instance

**Operational limit:** a long image pull with continued progress will not
be reallocated merely for being slow, and model downloads are checked by
their own byte-throughput watchdog. Logs for the failed start are required
to distinguish no capacity, image-pull stalls and repeated poor network
nodes. None of these improvements establishes A2V lip-sync or compiled
speed gains.

## 2026-09-26: reject compiled quality regression; verified-bootstrap eager control

The real `reference-compiled` monje smoke, seed 4242, 1280x720 at 24 fps
and 121 frames, reported 403.733 seconds inference and 425.529 seconds
total worker generation (`pipeline_reused=false`). The earlier eager
`reference` monje result documented in PR #228 reported 157.360 seconds
inference and 175.876 seconds total. Those timings are not a matched
cold/warm A/B, but compilation provided no measured first-run improvement.
The user-supplied new MP4 also shows substantially less mouth movement
than the preceding visual reference; both uploaded MP4s have the same
decoded audio. The preceding WhatsApp export lacks job sidecar metadata,
so it is not proof that all prompt/profile/seed inputs were identical.

**The controlled smoke and its direct Python submitter now reject
`reference-compiled` before allocating a GPU or submitting a Postgres
job.** Keep `reference` eager, the pinned 8+3 distilled schedule,
ancestral stage 1, image strengths 0.7/1.0, audio-frozen stages,
FP8_CAST/CPU offload, 121 frames and full voice padding unchanged for
the next baseline visual check. Merely changing node selection or host
timeouts does not modify denoising semantics. Do not change guidance,
checkpoint, number of steps, quantization, prompt or seed **during the
regression-control run**.

### Why the old startup readiness was unsafe

The worker could previously report `inference runtime prepared` when
the downloaded weight *files* existed, before `download_models.sh`
had completed the last file verification and written its atomic
`.bordell-installed-model-manifest.json`. That can overlap SHA-256
verification of large weights with the first inference and confound
cold-start benchmarks.

The new dedicated LTX Docker image sets
`LTX_REQUIRE_VERIFIED_MODEL_MANIFEST=true`. Its entrypoint removes the
per-start completion marker *before* launching uvicorn. The downloader
then validates the pinned repository/revision and whole expected file
set, including optional dev assets only when enabled; it publishes
the atomic verified receipt and then the fresh completion marker as
its last action. The production A2V and I2V prepare/readiness checks
require both artifacts and validate revision, exact file list, hashes
recorded in the receipt and current file sizes. They never rehash all
weights on each `/ready`; the bootstrap process alone computes/checks
full SHA-256. Neither task may claim Postgres work before completion.
Developer unit-test fixtures keep their explicit non-production gate
disabled. **This change is in worker/Docker source and requires a
new immutable image; updating only local PowerShell is insufficient.**

The smoke prints distinct `LTX_SMOKE_TIMING` lines for capacity start,
worker readiness, Postgres job and cleanup. Sidecar generation metrics
remain separate from the node allocation, image/model downloads and
model receipt SHA-256 verification. Reusing the *same* verified image
and the *same* short prompt/seed/input is mandatory when comparing
the old and new videos. Never report the cold-start plus verification
total as pure model inference, or a first-run compile result as warm
inference. A fresh Salad instance may still redownload the weights;
this fix deliberately does not claim persistent model cache across nodes.

### Applicability of the LTX talking-avatar blog

Source: https://ltx.io/blog/how-to-build-talking-ai-avatars-from-audio
(published for **LTX-2.3**, not the pinned LTX-2.5 code/checkpoint).
It recommends `modality_scale=3.0` as a lip-sync starting point,
video `cfg_scale=3.0` (or 2.0-2.5 if the face is static),
`stg_scale=1.0`, `rescale_scale=0.7`, `stg_blocks=[29]`,
and separately recommends audio CFG 7.0. These are **experimental
guidance changes**, not a free performance optimization. The existing
opt-in `guided` dev profile already implements the blog's video
guidance, but pinned upstream `A2VidPipelineTwoStage` constructs its
audio guider internally and cannot truthfully claim audio CFG 7.0.
A prior real guided test took 530 seconds inference and visibly
deformed the last frames. Forcing those settings onto the 8-step
**distilled** `reference` would also replace its `SimpleDenoiser`
recipe and confound the compiled regression comparison. Do not
silently apply blog guidance to the production/eager baseline. First
establish that the baseline restores the prior facial motion. Only
then test guidance in a separately named experimental profile and
accept it on phonetic mouth motion, identity stability and latency.
