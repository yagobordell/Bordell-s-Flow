# Whisper worker

Whisper Large V3 Turbo is a separately deployed speech-to-text worker.
It receives a verified audio input through R2, executes a Postgres-backed
inference job, and returns timing/text evidence. Worker configuration,
health checks, model bootstrap and safe replay follow the shared
inference contracts.

The managed B-pipeline orchestrates the transcription steps it needs.
There is no supported `run_phase5_alignment.py` command: that standalone
scene/shot-era runner has been removed.

Use `scripts/salad/manage_salad_worker.ps1` for worker deployment,
status and isolated manual service operations. Capacity for production
jobs belongs to the global controller, so avoid manually starting the
same service while it owns the pool. See
[Salad deployment](../operations/salad-stack-deployment.md).
