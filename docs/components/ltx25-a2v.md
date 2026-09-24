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

The native two-stage pipeline uses the dev transformer in stage 1 and the official distilled
LoRA in stage 2. Both stages condition on the **same frozen input audio**; the original
waveform is retained in the output. The avatar-specific video guider preserves dev CFG,
but disables extra modality guidance (`modality_scale=1`) and STG (`stg_scale=0`). Do not
copy CFG=1 from the separate fully distilled ComfyUI workflow into this dev pipeline.

Changing the avatar guider bumps `LTX_A2V_GENERATION_PROFILE` so old R2 results are not
replayed under new generation settings. The LTX Salad image tag must be rebuilt and
published before running a real smoke with this profile.

## Validation

Targeted real validation uses:

```text
scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1
```

The smoke verifies that the deployed worker uses the updated guider, but a passing MP4/audio
contract does not establish lip-sync quality: inspect the avatar's mouth movement against the\nprovided speech before accepting the visual result. The smoke does not replace production\norchestration.
Transport IDs, timings and artifact hashes from individual validation runs belong in Git history.
