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
