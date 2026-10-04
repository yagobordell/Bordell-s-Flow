# LTX 2.5 A2V FP8 residency and decoder benchmark — EXPERIMENT ONLY

Branch: `experiment/ltx25-a2v-fp8-residency-profiling-20261003` from
`main` at `357d289a00f4f10c87028b969861099d03819115`.
No production profile is reassigned; by default no production group image changes.\nAn explicit temporary reuse procedure below can change it and restore it.
The experiment service is intentionally **not** in the normal Salad stack service order.

## Source audit and constraints

- Pinned upstream Docker source:
  `Lightricks/LTX-2@a95ab856bf29407b6b066ede0abe1846050db56c`;
  model revision `6c7e5e573ac1667efc83407806fe9b0b93730e60`.
- Baseline for **matched-quality** comparisons: this repository's
  `reference` distilled A2V recipe, not the legacy `fast` or `guided`
  recipes. It uses **1280×720 / 24 fps public output** with deterministic
  internal 1280×768 grid padding, an audio-driven 8k+1 frame count, identical
  image, seed, prompt, frozen audio latents during both stages, 8 ancestral
  stage-1 plus 3 Euler stage-2 diffusion steps. Its reference image strengths
  are 0.7 and 1.0. Conditioning WAV is padded with silence solely to preserve
  the original speech tail. Its final output uses the decoded original audio,
  **not** generated audio. This is central to lip-sync safety.
- Production/legacy A2V normally uses FP8_CAST + CPU offload; historical
  memory-safe profiles use DISK and a safety reserve. These profiles, prompts,
  checkpoint filenames, steps, audio preparation and output sizes are left
  unchanged. Production I2V is an isolated different worker group already
  configured for FP8/no-offload.
- OffloadMode.NONE is supported by the pinned upstream A2VidPipelineTwoStage
  constructor, and is passed to PromptEncoder and both DiffusionStage
  instances. This **does not establish a VRAM fit**: audio/video VAE, tile
  activations and caching add A2V-specific memory demand. A failed
  32-GB CUDA allocation disqualifies that profile; do not shrink frames,
  change timesteps or offload audio conditioning away to rescue a benchmark.
- The original 5090 DiffVAE NATTEN fallback previously segfaulted.
  Eager SDPA stays the default. Opt-in Triton uses the same upstream probe as
  I2V, and is reported as effective only if the probe succeeds. Triton may
  change decoder floating-point rounding. It must pass decoded-media checks
  **and** human visual/auditory review independently.
- No new A2V NVFP4 profile yet. Although I2V supports an opt-in official
  prequantized transformer, NVFP4 A2V 5090 memory and audio/video parity
  have not been demonstrated. Do not silently substitute the I2V checkpoint
  or promote it without an independent study.

## Dedicated queue: current instructions (supersede older stop-production guidance)

The benchmark and production now use **different** PostgreSQL task names:
`video.ltx25.audio_to_video_experimental` versus
`video.ltx25.audio_to_video`. Existing `gpu.jobs.task` is TEXT, so **no SQL
migration and no cancellation of production B2 jobs** is necessary. The
experimental worker advertises `LTX_WORKER_TASK_MODE=a2v_experimental`; its
registry accepts only the experimental task, and its request validator rejects
non-experimental profiles. The smoke client submits experimental profiles
only to that task. Normal A2V is unchanged.

The production group and capacity controller **may keep running**, and
production's pending/running jobs are intentionally ignored by the experiment's
queue gate. The preflight requires **only** the experimental group to be
stopped and quiescent, and checks only runnable experimental jobs. Other
experiments in progress **cannot** share the same experimental group. This
isolates task ownership, not the physical Salad cost: simultaneous production
and experimental groups allocate independent GPUs. Do not start a separate
capacity controller.

**Required order:** update to this branch, build/push the experimental image
from this commit, `Prepare` to recreate the missing experimental Salad group
and pin the freshly published digest, then verify Status, isolation and
experimental queue gate before running the benchmark. The previously published
digest is unsafe because it still polls production A2V jobs. Do not apply the
historical "production must be stopped" steps elsewhere in this document.

## Temporary reuse of an existing stopped production group

If the separate experimental group cannot be created, the guarded script
`scripts/smoke/run_ltx25_a2v_benchmark_reusing_production.ps1` may temporarily
reuse the **already stopped** `ltx25` group. This is a separate, opt-in path:
normal benchmark execution continues to use the dedicated experimental group.

**Preconditions:** the normal A2V group is stopped/quiescent and still has its
expected immutable production image and `a2v` task mode; **both** production
and experimental Postgres task queues are idle; the Capacity Controller is
disabled and will remain disabled during the test; no other operator may
modify/start `ltx25` during this interval. A temporarily free advisory lock
is not proof that the controller cannot restart. This option temporarily
interrupts normal production availability, even though it does not consume
existing production tasks. Never use `-AllowControllerOverride`.

This guarded path never rebuilds Docker. It calls `Prepare` with the
already-published experimental digest, requires an exact expected original
digest and original task mode, and **preserves all of the actual group's
existing environment variables** while switching only `LTX_WORKER_TASK_MODE`
to `a2v_experimental`. The modified batch runner verifies the temporary image
and mode and rechecks **both** task queues before each experimental sample.
Afterwards the wrapper runs stop/restore in `finally`, restoring the
original image and `a2v` mode and verifying that the group is still stopped.
If this shell is forcibly terminated, recovery instructions are written to
`<OutputDir>/recovery.txt` before making any Salad changes. Never restart
production until `RESTORE_OK` has been independently confirmed.

Use the original production SHA actually reported by the read-only
`manage_salad_worker.ps1 -Action Status -Service ltx25` command, not a
hardcoded historical digest. For example, with the known pinned images:

```powershell
$Production = 'docker.io/yagobordell/ai-video-factory@sha256:08d7ee85fa24ec24d4e911c748156098f4c30d8b825dfeef2af0101af275d356'
$Experiment = 'docker.io/yagobordell/ai-video-factory@sha256:8c648c9d1481150d123f751d3546c3621822717c168f2a4ae5d8d1d134d45a5f'
uv run --no-sync python scripts/smoke/check_ltx_a2v_benchmark_jobs.py --include-production
uv run --no-sync python scripts/smoke/check_ltx_a2v_benchmark_isolation.py --env-file config/salad.env --reuse-production --expected-production-image $Production --expected-task-mode a2v
.\scripts\smoke\run_ltx25_a2v_benchmark_reusing_production.ps1 `
    -Audio <local-audio.wav> -AvatarImage <local-avatar.png> `
    -Prompt '<unchanged explicit prompt>' `
    -OriginalProductionImage $Production -ExperimentalImage $Experiment `
    -Profiles reference-bench-cpu,reference-fp8none -Repetitions 3 `
    -EnvFile config/salad.env
```

The explicit recovery command also exists independently:

```powershell
.\scripts\smoke\restore_ltx25_a2v_production.ps1 `
    -OriginalProductionImage $Production -ExperimentalImage $Experiment `
    -EnvFile config/salad.env
```

This operation is never automatic under the normal service stack. Do not
promote the experiment or merge this branch based on timing alone; a separate
lip-sync and decoded-media validation is still required.

## October 4 CPU warm-run fault and experimental mitigation

The controlled production-group reuse test completed its first two identical
reference CPU videos, then the third iteration failed during Stage 2 transformer
construction: upstream `X0Model(streaming_wrapper).eval()` raised
`TypeError: cannot unpack non-iterable FeedForward object`. A generic automatic
retry then segfaulted during the next attempt's CUDA cleanup. There was no
recorded CUDA OOM in the first failure. The third task has since been
operator-cancelled; do not resubmit its immutable ID.

The **experimental CPU-streaming profiles only** now use a narrow alternative
context: upstream `StreamingModelBuilder.build()` has already recursively put
the raw meta model in eval mode before installing streaming hooks, so the
experimental context does **not** traverse the complete live module tree
a second time. It preserves the original upstream builder and
`_streaming_model` teardown and sets only the outer wrapper's and `X0Model`'s
training flags to false. If upstream ever changes the meta-model eval
contract, the experimental guard refuses to proceed. No generation settings,
checkpoint, stage sigmas, seed, audio freezing, image strengths or decoder
backend change. Non-experimental profiles and I2V are untouched.

If the exact structural `FeedForward` failure still occurs, the experimental
task is terminal without an automatic retry in the same CUDA process. The
batch runner records the stderr traceback, saves an explicit failed attempt
in `manifest.json`, and refuses to continue when the terminal row lacks a
reliable error detail or reports structural corruption. **The mitigation is
not yet proof of GPU stability.** Rebuild and repin the experimental image
from the new branch commit before any new GPU comparison. Retain the two
original CPU MP4s and metadata separately; a new controlled batch should use
fresh run IDs and the same input image/audio/prompt/seed.

## Experimental profiles

| CLI profile | Original recipe | Transformer | Offload | DiffVAE |
| --- | --- | --- | --- | --- |
| `reference-bench-cpu` | reference, instrumented control | FP8_CAST | CPU | eager |
| `reference-fp8none` | reference, unchanged | FP8_CAST | NONE | eager |
| `reference-cpu-triton` | reference, unchanged | FP8_CAST | CPU | Triton |
| `reference-none-triton` | reference, unchanged | FP8_CAST | NONE | Triton |
| `static-bench-cpu` | static-reference **not equivalent to reference** | FP8_CAST | CPU | eager |
| `static-fp8none` | static-reference **not equivalent to reference** | FP8_CAST | NONE | eager |

The `static-fp8none` profile must be compared with the **instrumented**
`static-bench-cpu` control, not with the non-static reference. Never interpret
differences in static versus standard facial guidance as an offload effect.

The profiler is active only for these experimental identifiers. Metrics in
the sidecar and structured `LTX25_A2V_PHASE_METRICS` log include:
audio pre-processing; model build; prompt encoding; prompt model context
acquire/release; embeddings-processor construction; remaining prompt
execution (including tokenization); decoded input audio; AudioVAE encode;
both image conditioning stages; stage-1 and stage-2 diffusion; latent
upsampling; actual lazy video decoder consumption; encoder/mux exclusive
of decoder; wall-clock total; cold/warm reuse; peak allocated and reserved
CUDA memory. Output audio decode time is correctly reported as zero: the
original decoded waveform is forwarded to mux.

**Measurement limit:** the pinned upstream's Gemma context-enter operation
combines disk/host weight construction, host-to-device transfer and related
setup. `prompt_model_acquire_seconds` measures these together. Claiming
separate load and H2D timings without an upstream-level instrumentation
hook would be false. A transfer-specific follow-up should add synchronized
non-mutating hooks to the pinned upstream context in the *experimental*
image only and verify identical embeddings before using those numbers.
The current timing synchronizes CUDA around experimental phases, which
adds benchmark overhead and must be consistent between CPU and NONE.

## GPU validation gates (not satisfied by offline tests)

1. Leave normal production A2V and its capacity controller untouched.
   Confirm the experimental group is stably stopped and its **dedicated**
   PostgreSQL task has no runnable jobs. The isolation gate does not stop,
   mutate, or require stopping production. I2V is also unchanged.
2. Publish and pin **only** the new experimental image/group below. Ensure
   Salad and your local sources refer to this branch/commit and that the
   container has one 32-GB RTX 5090. No GPU tests were performed while
   preparing this branch.
3. Run `reference-bench-cpu` followed by `reference-fp8none`, **at least
   three repetitions each** (four by default: one cold, three warm) against
   exactly the same real source image/WAV, literal prompt, geometry and seed.
   The runner gives each attempt a distinct job ID to prevent Postgres/R2
   cache replay. It retains outputs, logs, metadata and a lifecycle manifest.
   It stops its own experiment group in `finally`, including partial starts.
4. Inspect `comparison.json`. Compare median and spread of **warm**
   generations, cold build, VRAM peaks, prompt, diffusion, decode and total
   wall clock, as well as capacity/readiness/stop. Equal offload recipe
   **should** yield the same decoded output; investigate any mismatches in
   decoded video hashes and reject mismatched decoded audio. Hashes of
   encoded MP4 alone are insufficient.
5. Two reviewers should perform side-by-side **visual and auditory** checks:
   identity, teeth/lips/jaw, blinking, facial shape, pauses, end-of-speech
   tail and phoneme alignment (especially /p/, /b/, /m/ and silence). Compare
   exactly the same timecodes for reference and opt-in variants. A speech
   synchronization metric, if used, only supplements human review.
6. Only after an acceptable eager variant exists, separately test Triton
   with the same paired inputs. Reject a Triton trial if the probe fell back
   to eager; any decoder-rounding differences require visual scrutiny.
   Repeat actual GPU runs across fresh Salad starts to measure startup
   variability. Never promote a single lucky run.

## Exact PowerShell commands (from the repository root)

First, switch locally to the remote experimental branch and confirm your
existing `config/salad.env`/R2/Postgres setup (do not commit secrets):

```powershell
git fetch origin
git switch experiment/ltx25-a2v-fp8-residency-profiling-20261003
uv sync --locked --extra dev --python 3.12
uv run --no-sync ruff check .
uv run --no-sync python -m pytest tests/unit/workers/test_ltx_a2v_experiments.py tests/unit/workers/test_ltx_a2v_runtime.py
.\scripts\salad\manage_salad_worker.ps1 -Action Validate -Service ltx25_a2v_experimental
```

After confirming no **experimental** A2V jobs are active, prepare only the
experimental service. Production B2 jobs and its capacity controller are independent. This builds and pushes the new image using the
branch's Docker source, then prepares the experimental group **stopped**:

```powershell
$Tag = "docker.io/yagobordell/ai-video-factory:ltx25-a2v-fp8-experiment-v1"
.\scripts\salad\manage_salad_worker.ps1 -Action Prepare -Service ltx25_a2v_experimental -Image $Tag -EnvFile config/salad.env -NonInteractive
docker buildx imagetools inspect $Tag
# Copy the published sha256 digest from the inspect output:
$Pinned = "docker.io/yagobordell/ai-video-factory@sha256:<PASTE_64_HEX_DIGEST>"
.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25_a2v_experimental -EnvFile config/salad.env
```

Then pass your **real** sample paths and literal prompt. Do not substitute
synthetic speech when evaluating phoneme-level lip-sync:

```powershell
$Prompt = 'A medium close-up talking head of the person in the reference image. The person speaks directly to camera with clear visible lip and jaw articulation precisely synchronized to the supplied speech audio. Lip sync matches every spoken phoneme. Natural blinking and restrained head movement. Static camera.'
$Out = 'data/output/deployment-validation/ltx25-a2v-fp8-review'
.\scripts\smoke\run_ltx25_a2v_benchmark_batch.ps1 `
  -Audio '.\data\input\avatar\monje.wav' `
  -AvatarImage '.\data\input\avatar\monje.png' `
  -Prompt $Prompt -Seed 4242 `
  -Profiles reference-bench-cpu,reference-fp8none `
  -Repetitions 4 -ExpectedPinnedImage $Pinned `
  -EnvFile config/salad.env -OutputDir $Out -NonInteractive
uv run --no-sync python scripts/smoke/compare_ltx_a2v_benchmarks.py "$Out/manifest.json"
.\scripts\salad\manage_salad_worker.ps1 -Action Status -Service ltx25_a2v_experimental -EnvFile config/salad.env
```

If CPU/NONE pass the quality gate, use a **fresh** `-OutputDir` and run
`-Profiles reference-bench-cpu,reference-cpu-triton,reference-none-triton`
with the same `$Prompt`, seed and inputs. For any failure, preserve the
partial manifest and logs; do not mask or replace CUDA OOM results. The
controlled runner only starts/stops `ltx25_a2v_experimental`, never
`ltx25` or `ltx25_i2v`. Separately review the official job logs and
Postgres state, and confirm group status after cleanup.

## Promotion block

All new profiles are opt-in. Automated tests and functional MP4/audio checks
are *not* lip-sync certification. No merge, production profile change,
experimental profile deletion or default dispatch switch is permitted until
real RTX 5090 evidence and human review establish a configuration worth
selecting. A draft PR may collect static checks without changing production.


## October 3 real-GPU findings and corrected retest

The first reference CPU/eager generation finished 137 frames (5.708s video)
from 5.460s real speech on the 32-GB Salad RTX 5090. It needed 1305.157s
overall, including 468.828s prompt (403.282s Gemma acquisition), 301.483s
Stage 1, 480.652s Stage 2 and 24.165s lazy video decode. Peak CUDA reserved
was 20438 MiB. A second generation began rebuilding Gemma although the
Python pipeline object was reused. The operator stopped the benchmark and
must resolve the second Postgres lease before any new GPU allocation.

The pinned upstream intentionally rebuilds/frees Gemma, diffusion models and
VAE models after each invocation. Pipeline reuse does NOT imply weight
residency. The experimental pipeline now caches only final Gemma connector
embeddings as bounded CPU tensors for identical, non-enhanced prompts within
one profile. It never retains the 12B Gemma model or the 22B transformer on
the GPU. First/cold samples still perform original encoding; true repeated
prompts skip Gemma on warm runs, with explicit cache-hit metadata. These warm
prompt timings are NOT representative of novel prompts in regular production.
Neither the measured peak nor the embedding cache establishes that FP8
no-offload fits the RTX 5090: a real test remains mandatory. Transformer
Stage 1/2 builds have different possible LoRA recipes; do not force weight
residency without an independent VRAM and output-parity test.

The batch runner now refuses to allocate or submit while ANY experimental A2V
Postgres job is pending, retryable or running (even if its lease has expired).
Production B2 jobs do not block the independent experimental queue.
Use the existing documented safe cancellation procedure for the interrupted
attempt; never cancel someone else's job. The opt-in
-ContinueOnTerminalFailure switch skips a failed profile only when its exact
persisted job is terminal failed and the entire A2V task queue is idle.
Active/expired leases, retryable failures, local media-validation errors and
inconclusive queue inspection must stop the batch. The
-MaxProfileTotalSeconds switch stops only remaining repetitions of an
already completed over-budget sample: it cannot terminate a running task.
Partial manifests require the comparator's --allow-partial flag and never
qualify for promotion.

Cost-controlled next run: after building a NEW experimental image, use a
new digest and one allocation. Test profiles in this order:
reference-fp8none, reference-bench-cpu, static-fp8none, static-bench-cpu,
reference-none-triton, reference-cpu-triton. Keep seed, WAV, image, literal
prompt, 1280x720/24fps fixed, ideally three repetitions. Opt-in switches
-MaxProfileTotalSeconds 360 and -ContinueOnTerminalFailure skip only safely
settled slow/failed profiles. An FP8 NONE OOM that remains retryable is NOT
safe to bypass; stop and settle its Postgres row instead of keeping the GPU
allocated. The comparator now matches static ONLY to static CPU and
reference ONLY to reference CPU, even when NONE tests run first.
Manual voice-tail, phoneme, identity and naturalness reviews remain mandatory.
This work stays on the draft experimental PR; NO production or I2V change.
