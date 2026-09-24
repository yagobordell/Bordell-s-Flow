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

The default **fast** profile uses the official distilled transformer directly in both
stages (no distilled LoRA), the upstream 8-step `DISTILLED_SIGMAS` schedule, a 3-step
refine pass, and CFG 1 with STG and isolated-modality guidance disabled. It retains the
supplied speech as frozen conditioning in both stages and muxes the original audio.
The explicit **dev** profile keeps the dev transformer, native 30-step schedule and
stage-2 distilled LoRA with dev CFG, while also disabling STG and modality guidance.

Both profiles use FP8_CAST, CPU block streaming and the stable eager-SDPA video VAE on the
RTX 5090. The latter remains necessary because the NATTEN path previously crashed on
this deployment; faster decoding or reduced CPU offload require separate GPU safety
validation. `model_load_seconds` measures pipeline construction, not all transformer
loading: upstream stages create/stream transformer weights inside `inference_seconds`.

Profile is part of the job identity to prevent R2 replay across modes. The new LTX Salad
image tag must be built and published before running a real smoke. The 1–2 minute target
for a five-second clip is an **acceptance target**, not a guaranteed runtime: compare the
actual `inference_seconds`, encode/mux and total against the prior 230.55-second run.

## Validation

Targeted real validation uses:

```text
scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1
```

The controlled PowerShell smoke defaults to `-Profile fast` and accepts `-Profile dev`
for a dev baseline using the same avatar and speech. The underlying Python smoke verifies
the selected checkpoint family, both stage step counts, CFG and frozen-audio contract.
For a five-second audio clip, pass `-MaxGenerationSeconds 120` to fail validation if
`total_elapsed_seconds` exceeds the two-minute acceptance target. This measures the worker's generation, not Postgres pending time or cold model downloads.

The smoke verifies that the deployed worker uses the updated guider, but a passing MP4/audio
contract does not establish lip-sync quality: inspect the avatar's mouth movement against the
provided speech before accepting the visual result. The smoke does not replace production
orchestration.
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

The worker image carrying the readiness fix is
`ltx25-a2v-torch211-cu128-eagersdpa-xet-fast-v9`. A passing MP4/audio contract still does not prove
lip-sync quality; visually inspect mouth movement against the supplied speech before accepting an A2V
profile.
