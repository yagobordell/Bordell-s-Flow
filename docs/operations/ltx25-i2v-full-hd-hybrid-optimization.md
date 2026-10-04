# Full-HD true B=2 I2V: hybrid GPU/CPU FP8 experiment

**Status:** Source-level experiment only; a fresh RTX 5090 paid trial, side-by-side visual review and host-memory monitoring are required. **DO NOT merge the historical mixed I2V/A2V PR.** A2V worker/profile, its concurrency and ongoing external optimizations remain untouched.

## Why the previous 1080p run was slow

The first native 1920×1080, true-GPU-B=2 trial with a resident FP8 transformer OOMed during stage2. Its two-prompt Gemma and true-B2 first stage succeeded; the latter needed ~29.7s denoising and ~7.7s model construction.

For the subsequent *successful* DISK-streaming full-HD pair, the attached 2026-10-04 Salad log measured: ~286s and ~297s to encode the two prompts, ~1,634s first-stage diffusion, ~924s second-stage diffusion, and 3,407.85s total (~56m48s). The successful job used 17.0 GiB CUDA peak allocated and emitted both 1920×1080 / 24fps / 121-frame MP4s without falling back to serial inference. However, the process RSS rose to ~37.4 GiB during stage2 and the disk-read counters grew from ~6 to >116 GB during the run. These results do **not** prove the host would handle all-pinned CPU weight buffers with only 40 GiB RAM.

## New implementation — I2V experimental mode only

1. **Stage1 is GPU-resident FP8 (NONE)**: restore the fast true-B=2 method already shown to complete the first full-HD stage. Two prompts are encoded separately as in the approved 720p baseline, before the transformer is constructed. Do not keep the stage1 model live across the upsampler or stage2.
2. **After disposing stage1, upscale both independent latents unchanged.** Release individual Gemma outputs, no-longer-needed stage1 conditioning and stage1 video tensors at safe boundaries. Preserve original stage1 audio latent, separate seeds and identical two-stage denoising sigmas.
3. **Build only an alternate stage2 wrapper lazily AFTER full-size image encoding.** Default `-FullHdStage2Mode cpu` uses LTX upstream's same FP8_CAST model and `OffloadMode.CPU`: blocks are pinned in host RAM **once** and transferred to the GPU per block across three full-resolution true-B=2 denoising forwards. This avoids the successful-but-slow DISK path, which re-reads blocks from the local checkpoint on every step. Only the stage wrapper is built, not a second Gemma/VAE pipeline.
4. **Host-memory prerequisite:** the isolated Salad *I2V-only* group requests **61440 MB** (Salad API resource field; often presented as ~60-GiB class), lowered from 65536 to improve RTX 5090 host availability. The PowerShell trial refuses the RAM-streamed 1080p experiment if the tracked I2V manifest requests less than 61440. Memory units are reported as MB by Salad's SDK; verify the remote API field and available host headroom rather than treating its label as a guaranteed GiB allocation. This is an UNVALIDATED experimental minimum, not proof that the full pinned FP8 blocks fit; leave A2V unchanged and check for memory pressure or swap in the per-stage worker logs.
5. **Explicit fallback for investigation:** `-FullHdStage2Mode disk` keeps the bounded two-slot CPU DISK streamer for stage2 only. It does *not* silently downgrade an attempted CPU job. 720p B2 remains unchanged and fully GPU resident. Full-HD always retains genuine GPU forward batch=2 and no sequential fallback.
6. Log the selected stage1/stage2 offload policy, stage2 CPU buffer policy, per-stage elapsed/build time, per-forward VRAM and host counters. The report labels the hybrid mode as `stage1:none,stage2:cpu` or `stage1:none,stage2:disk`.

The pipeline does not change the transformer checkpoint, FP8_CAST recipe, final 1920×1080 geometry (internal 1920×1088 padded/cropped grid), 121 frames, 24fps, sampler schedules, independent random seeds, conditioning arithmetic or VAE. CPU versus DISK loading should not change numerical model weights; generated videos still require direct quality verification.

## Failure postmortem: old Docker rejected hybrid mode before inference

A 60-GiB hybrid attempt on 2026-10-04 spent 775 seconds downloading models
(`cache_hit=false`), reached worker ready and claimed its Postgres job, then
failed in `Batch2Parameters.model_validate`: the installed Docker rejected
`full_hd_stage2_mode=cpu` as an unknown field. The host's newer smoke
client cannot update Python code in an old immutable Docker digest.
**No GPU denoising happened**, and this does not measure hybrid performance.
Afterwards the PowerShell finally Stop was blocked by a Capacity Controller
advisory lock, so the group's real state must be verified immediately
through a read-only Salad API request or the Salad console. Never use an
override without confirming exclusive operator control and no other runs.

To prevent this expensive mistake, the I2V-only PowerShell 1080p smoke
now runs `scripts/smoke/verify_ltx25_i2v_hybrid_image.py --image <digest>`
**before** its paid Salad Start. This executes Python with
`docker run --rm --network none --entrypoint python` inside the **exact
pinned image** and verifies the hybrid Pydantic schema, backend and lazy
stage2 pipeline signatures. Docker may pull the image locally if absent,
so the check can take time/bandwidth but uses **no paid Salad GPU**. A
previous all-DISK image MUST fail. The `-CheckInputsOnly` path remains
fast and never starts Docker; it alone does not certify the image.

Fix procedure: check that the group is really stopped and the singleton
controller is out of the way; build/push the immutable Docker **from the
reviewed hybrid branch** if needed; use the offline exact-image ABI check;
Prepare ONLY the stopped I2V group at the existing 61440-MB experiment
budget; verify its remote image and RAM; then submit a NEW RunId. Failed
transport jobs are not reused. A2V must not be stopped or mutated merely
to recover this experiment.

## Free validation and next paid decision

- Use the branch-scoped I2V CI first: Ruff, paired worker contracts, lazy-stage build ordering, exact FP8 policy, no hidden B1, Full-HD geometry and PowerShell safe preflight.
- A **new Docker digest** is required for the hybrid worker-source change, unlike this later **manifest-only 61440-MB adjustment**, which does not require rebuilding an already correctly built HYBRID image. Re-Prepare ONLY the stopped I2V Salad service with the new digest and 61440-MB host RAM; verify remote resources before paying.
- New unique RunId and two preferably distinct 1920×1080 source PNGs; use `-Width 1920 -Height 1080 -AcceptFullHdBatch2OomRisk -DisableStageReuse -FullHdStage2Mode cpu -CheckInputsOnly` first. Compare same-seed/prompt outputs, technical validation, max GPU and host memory, CPU major page faults, per-stage latency and startup cost against the 56m48s DISK baseline.
- With the 61440-MB budget, CPU pinned weight startup may still exhaust host RAM or be affected by poor storage; **do not assert the time or memory problem is solved** until real hardware evidence supports it. Do not auto-retry expensive CPU failures in DISK mode and do not enable this experiment in the normal B2 dispatcher.

**Quality gate:** visual review against matched 1080p B=1 reference frames and lipsync checks remain separate; the observed latest paired test used the SAME initial PNG hash for both outputs, so acquire two distinct sources for broader visual acceptance.


## Extra fail-closed guards for the next 60-GiB trial

The local source-only `-CheckInputsOnly` does not verify Docker or the
remote group. Before **any paid Start**, the Full-HD PowerShell wrapper now:

1. Executes `verify_ltx25_i2v_hybrid_image.py --image <immutable-digest>`
   against the exact locally available or pulled Docker. It imports
   `Batch2Parameters` from **inside** that image and checks the backend and
   lazy-stage signatures. A historical DISK-only image fails here.
2. Calls `Status` for both isolated groups, without changing them.
3. Reads the **remote** I2V group through Salad's describe-only endpoint using
   `verify_ltx25_i2v_remote_config.py`. Requires the same immutable digest,
   batch2 task flag, I2V task mode, at least 61440 MB of remotely reported
   memory, and stable stopped state with no pending change. If the remote
   group is different, no GPU starts. The remote image digest matched to the
   locally inspected digest is the deployment ABI proof; no unsupported
   direct access to remote worker internals is assumed.

Free explicit checks from the experimental checkout:

~~~powershell
$EnvFile = ".env"
$Pinned = "<replace-with-real-docker.io/yagobordell/ai-video-factory@sha256:...>"
python scripts/salad/inspect_capacity_lock.py
.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25_i2v -EnvFile $EnvFile -NonInteractive
python scripts/smoke/verify_ltx25_i2v_hybrid_image.py --image $Pinned
python scripts/smoke/verify_ltx25_i2v_remote_config.py --image $Pinned --env-file $EnvFile --minimum-memory-mb 61440
~~~

The lock inspection reports **only read-only** PostgreSQL lock-holder PID,
application name when PostgreSQL exposes it, and the latest controller
health record. The record may not identify the actual human or a currently
active job; coordinate with the controller owner before any intervention.
Never call `-AllowControllerOverride` merely because cleanup failed. Check
the Salad console or use the two `Status` commands when actual account
state is needed; repository code alone cannot determine live GPU charges.

For a paid trial, the wrapper retains the original generation error if Stop
fails. It performs a read-only I2V Status and capacity-lock inspection and
prints **I2V CLEANUP UNVERIFIED** rather than claiming an idle GPU is stopped.
When Start itself does not confirm success, cleanup refuses to blindly Stop a
possibly foreign group and demands operator reconciliation. This is a
fail-closed alarm, not a guarantee of automatic cleanup under a competing
controller. A truly guaranteed owner-scoped automatic shutdown would require
coordinated controller-issued per-run group leases; implement that in its
separate concurrent multi-operator workstream, not this historical mixed PR.

These client-side and operational changes do **not** change the LTX worker
image. A previously published genuine hybrid Docker that passes the exact
offline digest check can be reused. Never rebuild solely because the RAM
manifest changed. If the installed immutable image fails the ABI probe, a
new image from the reviewed hybrid source is necessary.


## 2026-10-04 1080p CPU-stage2 trial: stage2 sigma name collision

The next paid test got through bootstrap with I2V instance version 17 and
reached the real B=2 GPU-resident stage1. The attached worker CSV establishes:

- Both individual Gemma prompt encodings succeeded.
- Stage1 reported `forward_batch=2`, ~31.89 s transformer build and
  ~26.79 s diffusion. Upcaling and full-size image conditioning completed.
- The lazy stage2 factory selected `offload=cpu cpu_slots=all`.
- BEFORE stage2 diffusion started, `paired_i2v.py` passed a
  `DiffusionStage` instance through its `sigmas` keyword, because the local
  `stage2` variable had been overwritten by `stage2_factory()`. Indexing
  `sigmas[0]` raised `TypeError: 'DiffusionStage' object is not subscriptable`.
- This was a Python naming collision, not a stage2 OOM or a CPU-streaming
  performance measurement. The outer script confirmed Salad I2V
  `status=stopped pending=False` after the failure.

Corrected by keeping a distinct `stage2_sigmas` tensor initialized from
the original `STAGE_2_DISTILLED_SIGMAS` throughout the same B=2 sampler.
No steps, diffusion equations, precision, resolution or offload policy were
altered. A CPU-only AST regression gate plus a GPU-optional mocked stage
check now forbid passing a stage object where the second-stage sigma tensor
is required. The offline Docker preflight also checks an explicit patched
stage2 ABI marker AND parses the installed source AST; an older hybrid image
that passed the previous signature-only ABI check is no longer accepted.

**New immutable Docker build is REQUIRED for this worker-source fix.**
The formerly prepared image must not be reused for a paid run even though
its Pydantic hybrid field and remote resource checks succeed. Start only
after a fresh build from this reviewed commit, full I2V CI green, a clean
offline Docker ABI verification and remote read-only image/RAM match. Use a
new job RunId and operator approval. Stage2 CPU RAM/VRAM fit and end-to-end
runtime remain unproven.

## 2026-10-04: progressive host-RAM optimization (isolated experimental I2V)

The corrected 1080p hybrid trial generated **two genuine B=2 videos** in
`297.762588s` inference, peak CUDA allocated `24,786,865,664 bytes`,
using unchanged FP8 stage1 on GPU and all-block CPU streaming for stage2.
Salad stopped the I2V group afterwards. Stage2 denoising was ~53 seconds;
the peak process VmHWM was ~60.9 GiB and swap was observed. The two uploaded
source keyframes have the *same* SHA256 despite distinct seeds and prompts.
Visual parity with separately generated B=1 controls is still unverified.

The pinned upstream StreamingModelBuilder allocates the final per-block CPU
buffers *and then loads every raw checkpoint block into a second state dict*
before filling those buffers. This doubles transient model weight residency
at the stage2 construction boundary. The opt-in Docker-only
`patch_i2v_progressive.py` keeps all of the original upstream pinned buffers,
block layouts, FP8 quantization policy and GPU streamer, but substitutes
a per-block `load_state_dict` using the **same restricted upstream SD ops**.
Each raw block's tensors are cleared after copying their exact bytes into
the original destination views. It refuses mismatched transformed keys,
weight-retaining registries or the presence of unapproved LoRAs; there is
no silent DISK or sequential downgrade.

Only `DirectLTX25Backend._full_hd_streaming_stage2(mode="cpu")` enables this
mode. The default builder and other modalities/profiles retain the original
code path, including 720p I2V, I2V DISK and all A2V. The original second-stage
denoising loop, all-block CPU residency, H2D buffers, batch=2 and exact
FP8 model arithmetic remain unchanged. Startup block reads may have
additional metadata/header overhead; the effect on **total** latency must
be measured, not assumed away. There is no theoretical change to the
stage2 *forward* computation.

**Free acceptance:** pass the scoped CI including byte-for-byte target-buffer
unit checks, per-block raw-weight lifetime checks, default-off/A2V isolation,
the patch's exact application to pinned upstream LTX commit
`a95ab856bf29407b6b066ede0abe1846050db56c`, and Python syntax compilation.
The paid smoke's offline exact-Docker-digest probe rejects the *previous*
working hybrid image: only a NEW immutable image including the progressive
builder patch and I2V-only opt-in is admissible.

**Paid hardware acceptance only after explicit operator approval:**
confirm image digest and remote 61440 MB, both group statuses and controller
ownership, then run one controlled experiment with different native Full HD
images and meaningful prompts. Record all existing phase timings and now
the cgroup `memory.max`, `memory.peak`, swap peak, OOM/high events when
readable, process VmHWM and major page faults. A candidate RAM goal is
**VmHWM <=48 GiB**, with no uncontrolled swap or cgroup OOM; this is a goal,
not a guarantee. Compare stage2 denoising (~53s) and pair inference total
(297.76s) against this baseline on matching inputs/hardware, and verify
neither regresses beyond run-to-run noise. Compare both resulting videos
against **B=1 runs with matching seeds/prompts/original images** before
promoting outside the isolated experiment. If any quality or latency gate
fails, keep the original pinned hybrid image as the known reference; do not
silently activate the memory change.


## 2026-10-04: Full HD I2V pair output latency: one decoder build, safe backend

**Confirmed root cause of the "missing NATTEN" warning:** this repository
**deliberately** disables NATTEN for the RTX 5090 in
`_force_diffvae_eager_sdpa`. A previous production-style landscape-grid
NATTEN decode triggered a native segmentation fault, despite the library
being installed in the Docker image. The slow eager-SDPA fallback is the
current *safety-pinned baseline*. Do NOT delete this override or imply that
installing/re-enabling NATTEN alone is a safe speed optimization. Triton
remains a separately testable candidate; it is not selected for this
experimental full-HD pair by this change because visual and hardware
equivalence are unproven.

**New safe latency change:** For the experimental 1920x1080 B2 task only,
the output encoder temporarily installs a `SharedSequentialVideoDecoder`
while it consumes the original pair of lazy video iterators. It constructs
the *same prepared upstream VAE decoder and same eager-SDPA backend once*
instead of rebuilding it separately for each output. Each independent
decoder call still consumes its **original advanced seed-specific generator**,
original 1-sample latent and original tiled configuration. The second clip
does not start until the first iterator is entirely consumed, avoiding
mixed VAE streams/frames. The identical `gpu_model` disposal context
remains open only across these two sequential clip decodes; it is disposed
and the pipeline's original decoder restored in `finally` on both success
and error. The 720p pair, all single-video generation, all A2V and the
transformer stage loops remain unchanged.

**Free checks:** the no-GPU I2V workflow compares mock decoded bytes from
the shared VAE against two independent original-style VAE constructions,
including distinct advanced RNG streams; checks the lazy construction,
interleaving rejection and exception cleanup. The offline exact-image
preflight now additionally requires the shared I2V decoder and the
`is_full_hd` guard while verifying eager backend safety is retained.
An image without this optimization fails before billed Salad Start.
Mock byte parity proves the plumbing and generator semantics, **not**
real pixel identity after an actual DiffVAE decode.

Logs now distinguish `LTX25_I2V_SHARED_VAE_BUILD` from
`LTX25_I2V_SHARED_VAE_CLIP_DONE` and `LTX25_I2V_SHARED_VAE_RELEASE`.
The per-clip wall time includes the downstream MP4 encoder's consumption,
so do not misrepresent it as GPU-only decoder kernel time. Historical
baseline was 297.762588s total for B=2 1920x1080 / 121 frames; the
second VAE was previously loaded around the start of the second video.

**Required real acceptance:** build a NEW immutable I2V Docker from the
green exact branch HEAD; run its offline ABI probe, re-Prepare only a
stopped I2V group with a verified digest and >=61440 MB, and obtain
explicit operator authorization before a new paid trial. For a meaningful
comparison, keep the same two full-HD PNGs, prompts, seeds, exact eager
backend, stage2 CPU policy and image hardware as baseline. Compare
`LTX25_I2V_SHARED_VAE_BUILD`, the two output decode/encode times,
stage1/stage2 forward times, process/cgroup memory and full total against
297.762588s. Decode both result videos next to corresponding matched B=1
reference outputs; a stable-looking pair by itself cannot establish B=1
visual equality. Do not promote to production or assert speedup until
measured. Treat Triton NA and kernel/compile changes as a **different
explicitly gated** experiment, not part of this quality-preserving change.


## Experimental Triton DiffVAE acceleration (NOT production-approved)

The last RTX 5090 shared-VAE + progressive-RAM 1080p true-B2 run took
**253.765606s** for two videos (vs a non-identical-prompt prior baseline
297.762588s). Its output decoder/MP4 segment took about **88.23s**.
This is an optimization target, not evidence that Triton will be faster.

**Native NATTEN stays explicitly disabled.** It previously segfaulted on the
RTX 5090. Instead, test only upstream LTX's existing
`fallback_na.triton_na.na3d` with upstream `CHUNKED_EAGER` DiffVAE. This
keeps every model weight, sigma, image-conditioning, sampler, RNG generator,
stage1/stage2 policy, tiling, individual-clip decoding and shared VAE lifecycle
unchanged. It changes *only* the neighborhood attention implementation
used by the DiffVAE and therefore its numerical results are not guaranteed
pixel identical until tested.

The task request field `vae_backend` defaults to `eager`. Only the
explicit `triton_experimental` value on 1920x1080, 24fps, 121-frame true B2
with all-block CPU stage2 is accepted. B1, 720p and A2V are unaffected.
The strict setting is threaded through the dedicated CLI, PowerShell
wrapper, Pydantic task model and backend. It is recorded in the submitted
job fingerprint, output report and provenance. No implicit fallback is
permitted if Triton is unavailable or fails; this keeps the result truthful.

**Safety before billed startup:** The exact immutable Docker digest is
inspected offline with `--require-triton`. The image must import the Triton
NA kernel and contain the strict API and per-request GPU probe. Before
beginning the timed B2 inference, the worker launches a bounded independent
CUDA subprocess testing Triton against the pinned eager reference for
three BF16 3D-attention shapes, including shifted-boundary windows. It checks
finite outputs and tensor closeness (absolute/relative tolerances 0.04).
An import failure, kernel fault, timeout or disagreement aborts the
experiment without submitting inference; only the child can segfault.
The probe's duration is reported separately from B2 elapsed time and
may still incur Salad billing after startup. Small-kernel agreement cannot
prove full-video equivalence or long-run stability.

**Mandatory controlled eager comparison:** Triton trials must supply two
existing baseline EAGER B2 MP4s (`-ReferenceA`, `-ReferenceB`), from
the *same directory* with original job-request.json and
reference-provenance.json. Preflight checks their exact input image hashes,
prompts, seeds and original eager backend before any GPU starts. Once
both Triton videos have been produced, the client compares all 121 decoded
frames independently with `ffmpeg` PSNR and SSIM and writes
`backend-quality-comparison.json` plus per-frame stats to the candidate
run directory. No arbitrary threshold auto-approves perceptual quality:
the result remains `HUMAN_SIDE_BY_SIDE_REVIEW_REQUIRED`.
Visual side-by-side inspection is indispensable to validate human motion
and any temporal artifacts beyond frame metrics. Compare paired stage
forward seconds, output time, peak VRAM/RAM and potential kernel
initial-compilation latency as well.

Example local free checks, after building and publishing a NEW immutable
Docker from the current green experiment branch:

~~~powershell
$Trial.ExpectedPinnedImage = $Pinned
$Trial.ReferenceA = "data/output/deployment-validation/ltx25-i2v-batch2/i2v-optimized-20261004133012/batch2-a.mp4"
$Trial.ReferenceB = "data/output/deployment-validation/ltx25-i2v-batch2/i2v-optimized-20261004133012/batch2-b.mp4"
$Trial.RunId = "i2v-triton-$(Get-Date -Format yyyyMMddHHmmss)"
.\scripts\smoke\run_ltx25_i2v_batch2_trial.ps1 @Trial -Width 1920 -Height 1080 `
    -FullHdStage2Mode cpu -AcceptFullHdBatch2OomRisk -DisableStageReuse `
    -VaeBackend triton_experimental -CheckInputsOnly
python scripts/smoke/verify_ltx25_i2v_hybrid_image.py --image $Pinned --require-triton
~~~

Only after the free gates pass, the user explicitly chooses to start the
paid test with the same PowerShell command WITHOUT `-CheckInputsOnly`.
This wrapper retains its stopped-group, remote RAM, capacity-controller
locking and automatic owner-checked cleanup requirements.

If the Triton results fail stability, exceed eager runtime, or display
any quality regression, leave the approved eager configuration unchanged.
Do not promote this branch to main on an unverified video comparison.


## I2V B2 continuous RAM telemetry (2026-10-04)

Every isolated B2 **inference attempt** now starts an independent, read-only
`I2VMemoryTrace` background sampler at **2-second intervals**. It records
wall-clock UTC and monotonic elapsed time, active generation phase, process
RSS/HWM, swap, major page faults and process disk reads; it also samples
container cgroup memory usage/limit/peak, cgroup swap, anon/file cache
and OOM events **only when the OS exposes the corresponding files**.
It never synchronizes the GPU, runs CUDA operations, adds model allocations,
or modifies weights, timing policies, seeds, latents or sampler arithmetic.
Phase markers trigger one extra CPU /proc snapshot so very short phases
are not missed. Phases distinguish setup, prompt/conditioning, stage1
transformer build/denoise/forwards, upsampling, stage2 build/denoise/
forwards, separate A/B VAE+MP4 processing and output completion.

The cgroup discovery now also maps `/proc/self/cgroup` through
`/proc/self/mountinfo`, accounting for cgroup namespace / bind-mount
subtree roots before attempting conventional fallback paths. When none of
the memory controller paths is exposed, `cgroup_available` stays **false**
and `cgroup_unavailable_reason` is recorded; **process VmHWM must not be
misrepresented as container memory usage**. If readable, sampled container
headroom is `memory.max - memory.current`, and kernel-reported
`memory.peak` is separately labelled: that kernel peak may include
activity before this attempt and is not identical to sampled per-job peak.
The v1 `memsw` field is memory+swap, not pure swap; interpret accordingly.

Live progress and crash survivability: every sample is logged as a
structured `LTX25_I2V_RAM_SAMPLE` record (and a summary is logged at
`LTX25_I2V_RAM_TRACE_FINISH`). Thus an OOM or other failed attempt still
leaves a partial trace in Salad logs; `failed_attempts` stores the
summary when it returns a report. No batch failure is caused merely
by absent or unreadable RAM telemetry.

Successful attempts include a complete `memory_trace` object (samples
plus summary and phase markers) inside the EXISTING atomically published
`batch2-report.json` sidecar. The client additionally exports
`memory-trace.json` and `memory-trace.csv` next to the usual
`experiment-summary.json` for plotting; these are local derivatives
of the durable report and do not introduce extra R2 / queue sidecars.
The read-only collector's 2-second CPU sampling overhead is deliberately
small but **must still be measured** against existing 190.52-second Triton
results before claiming truly zero runtime impact. Native CPU process
metrics cannot expose GPU VRAM; existing CUDA stage checkpoints still
record VRAM and peak allocator stats separately.

The Triton CUDA compatibility probe executes in an **independent child
process BEFORE the timed B2 inference attempt** and is represented by its
separate `triton_gpu_probe_seconds` field rather than folded into
`memory_trace`. Salad worker bootstrap is likewise outside the trace.
This separation makes comparisons with the previous reference
unambiguous.
