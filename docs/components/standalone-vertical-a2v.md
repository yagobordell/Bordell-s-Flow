# Standalone vertical LTX A2V (9:16)

This helper generates talking-head videos from an existing JPEG/PNG and MP3/WAV. It does not run B1/B2/B3, TTS, image generation, CTAs, subtitles, or either professional/casero flow.

## One-command usage (PowerShell)

Plan without paid inference:

    .\run_vertical_a2v.ps1 -Image ".\inputs\imagen.jpeg" -Audio ".\inputs\voz.mp3" -Script ".\inputs\guion.txt" -PlanOnly

Generate:

    .\run_vertical_a2v.ps1 -Image ".\inputs\imagen.jpeg" -Audio ".\inputs\voz.mp3" -Script ".\inputs\guion.txt"

Optional parameters: -OutputDir, -Prompt, -Seed, -RunTag, -NativeOutput.
The Python equivalent is:

    uv run --locked --extra dev python scripts/pipeline/run_vertical_a2v.py --image inputs/imagen.jpeg --audio inputs/voz.mp3 --script inputs/guion.txt

The TXT script is optional: its hash is recorded for traceability. It is NOT sent to LTX and is NOT used to align words. Audio is the authoritative speech input. Correcting the TXT does not cause a new inference.

## Technical contract

- Native generation: **576x1024 pixels, 24 fps, 9:16**, aligned to LTX's 64-pixel spatial grid. Uses the current A2V reference worker/profile and Postgres/R2 routing, not the professional/casero planning pipeline.
- Final MP4: **1080x1920 H.264/AAC (9:16)** by default, FFmpeg Lanczos upscaled from native 576x1024. Upscaling does not create more generative detail. Choose -NativeOutput for a 576x1024 final.
- Image: maintain the entire source composition on a 9:16 canvas with a blurred version of the source behind it. No face-damaging landscape-to-portrait center crop.
- Audio: decode MP3/WAV to deterministic 24 kHz stereo PCM, find silent intervals with FFmpeg silencedetect, and make clips shorter than 11.25 seconds (the reference A2V worker rejects audio exceeding 12 seconds). If no natural silence exists, the split is deterministic and a warning/count is shown. Such cuts can occur within a spoken word.
- Composition: keep the full original decoded audio as the final track instead of combining independently encoded A2V AAC pieces. Video segments are stitched in timeline order; model continuity between independent clips cannot be guaranteed.
- Resume: manifests include an immutable SHA-based generation identity, per-fragment audio hash and verified video SHA. Locally verified clips are reused; incomplete A2V jobs preserve their Postgres job IDs so the worker can replay persisted results. Never mutate existing Postgres jobs manually. If a job ended in a terminal failure, fix the cause and explicitly start a new generation with a different -RunTag.
- Capacity: no direct group Start/Stop/resize calls. Relies on the existing healthy singleton Capacity Controller, Postgres and R2. Checks controller health before submitting paid work. -PlanOnly calls neither Salad nor the inference provider.

Default output:

    data/output/vertical-a2v/<audio-name>-<fingerprint>/
        video_final_9x16.mp4
        manifest.json
        work/master.wav
        work/reference.png
        work/part-001.wav
        part-001/avatar_segment.mp4
        part-002/avatar_segment.mp4

## Validation

Offline tests:

    uv run --locked --extra dev python -m pytest tests/unit/test_run_vertical_a2v.py -q

**GPU limitation:** the worker's input schema supports 576x1024, but a real GPU test for vertical A2V has NOT been executed by this change. Run a short real paid validation on Salad and inspect lip sync, identity preservation, start/end geometry, camera framing and joined-clip transitions before relying on bulk production. The existing landscape video pipeline is intentionally unchanged.
