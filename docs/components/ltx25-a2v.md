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

The worker does not infer timeline ownership itself.

## Output

A2V returns a single video artifact through the normal inference envelope. Production geometry remains
aligned with the 16:9 LTX pipeline and downstream composition.

Request identity includes the inputs and generation-defining parameters so a repeated segment can use
the shared replay/idempotency path.

## Runtime

A2V and normal image-to-video share the same LTX service and resident model family. They do not require
separate Salad groups.

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

The smoke verifies that the deployed worker uses the updated guider, but a passing MP4/audio
contract does not establish lip-sync quality: inspect the avatar's mouth movement against the
provided speech before accepting the visual result. The smoke does not replace production
orchestration.
Transport IDs, timings and artifact hashes from individual validation runs belong in Git history.
