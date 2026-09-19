# Landscape 720p + Real-ESRGAN 1440p migration

## Target production path

```text
Phase 6 landscape keyframes
  -> Phase 8 LTX 1280x720 @ 24 fps
  -> Real-ESRGAN_x2plus clip upscale 2560x1440 @ 24 fps
  -> Phase 9 Remotion 2560x1440 @ 24 fps
  -> H.264 + AAC final mux
```

The original LTX clips remain canonical Phase 8 intermediate artifacts. Upscaled clips are separate deterministic artifacts and are the only clip set consumed by the production Phase 9 path.

## LTX 720p grid compatibility

The pinned LTX-2.5 `DistilledPipeline` is a two-stage pipeline whose official code rejects target dimensions that are not multiples of 64. Therefore a literal two-stage model call with height 720 is impossible (`720 % 64 != 0`).

The public production contract remains exactly `1280x720 @ 24 fps`. Inside the LTX worker only, the canonical 16:9 keyframe is resized without aspect distortion to 1280x720, extended vertically with deterministic edge padding to the nearest valid two-stage canvas (1280x768), passed to the unchanged validated LTX pipeline, and the decoded pixels are center-cropped by 24 px at the top and bottom back to exactly 1280x720 before H.264 encoding. No temporal samples, seeds, steps or model settings are changed.

This compatibility behavior is part of `ltx25-distilled-a95ab856-fp8cpu-gridpad-v2`, so the previous vertical/profile cache cannot be reused accidentally.

## Minimal architecture change

The existing Phase 8 manifest/resume design and generic Salad inference core are reused. A dedicated `realesrgan` worker/task is added with its own queue, service entry and deterministic application job identity. The upscaler stage performs cache/R2 validation before queue submission and persists a separate manifest.

The new stage is spatial only: source frame order, frame count, fps and duration are validated before accepting an output. No frame interpolation or temporal filtering is permitted.

Phase 9 keeps `ShotTiming` as temporal source of truth. Its composition plan moves to 2560x1440 and reads the upscaled clip manifest/output set, so captions, overlays, transitions and progress UI are rendered at native 1440p rather than being upscaled.

## Real-ESRGAN profile

Use official `RealESRGAN_x2plus` for the canonical 2x path. The official Real-ESRGAN model zoo identifies it as the general-image X2 model, and the official video inference script instantiates it as an RRDBNet with `scale=2`. No external inference API is used.

The worker owns the model weights inside the deployment boundary and records model/profile, source SHA-256, source/target dimensions, fps and codec parameters in deterministic request identity/metadata.

## Cache and lifecycle invariants

- cache is checked before GPU allocation;
- source SHA/model/profile/target size changes invalidate cache;
- one missing clip submits only one upscale job;
- cached success is bound to R2 artifact identity/hash, never local file existence alone;
- Real-ESRGAN uses scale-to-zero and participates in global stop, queue cleanup and replicas=0 guards;
- Phase 8 resume semantics remain unchanged: only pending/running transports are active resume work.

## Validation gates

CI must cover 16:9 dimensions, request fingerprints, cache invalidation, worker contracts, lifecycle cleanup, Phase 9 2560x1440 composition and anti-regression against vertical production defaults.

Before merge, a real Salad run must verify 1280x720 LTX clips, 2560x1440 upscaled clips, a 2560x1440/24fps H.264 + AAC final video, visual quality, zero-GPU replay and final stopped/replicas=0/clean queues.
