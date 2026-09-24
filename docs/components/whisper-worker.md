# Whisper worker

Whisper Large V3 Turbo provides word-level alignment for the canonical narration asset.

## Boundary

```text
narration.wav
   -> R2 input
   -> Salad Whisper worker
   -> words.json
   -> NarrationWord[]
```

The worker uses the shared inference core for R2, Postgres leases, replay and request validation. Its
model package owns only Whisper settings, runtime preparation and transcription task handling.

The source script is not injected as decoder prompt context. The audio plus explicit language hint
drive transcription; downstream validation compares timing/text evidence against the production
contract.

## Deployment

The service definition is `whisper` in `deploy/salad/services.json`. GPU class, image, replica capacity and model environment are manifest-owned.

Model bootstrap uses the shared download watchdog and validates the local snapshot before the worker
becomes ready.

Run alignment directly for debugging with:

```powershell
python scripts/pipeline/run_phase5_alignment.py
```

Normal production uses the controlled wrapper and end-to-end runner. Service lifecycle operations use
`scripts/salad/manage_salad_worker.ps1`.

OpenAI transcription remains an optional compatibility provider in code; it is not the normal Phase 5
alignment path.
