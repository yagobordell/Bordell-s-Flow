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

## Validation

Targeted real validation uses:

```text
scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1
```

The smoke is for validating the worker capability, not for replacing production orchestration.
Transport IDs, timings and artifact hashes from individual validation runs belong in Git history.
