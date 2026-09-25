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

The controlled PowerShell smoke defaults to `-Profile fast` for backwards-compatible
operator behavior and also accepts `-Profile reference` and `-Profile dev`. Reference
validation verifies the selected checkpoint family, both stage schedules, sampler
semantics, per-stage image strengths, frozen audio, upward temporal-grid padding and the
decoded voiced span at the end of the generated MP4.
For a five-second audio clip, pass `-MaxGenerationSeconds 120` to fail validation if
`total_elapsed_seconds` exceeds the two-minute acceptance target. This measures the worker's generation, not Postgres pending time or cold model downloads.

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
