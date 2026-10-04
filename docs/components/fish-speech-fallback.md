# Fish Speech: validation and fallback provider

Fish Speech runs as an independent Salad worker with Postgres-managed jobs
and R2-backed inputs. It remains available for supported voice configurations
and for isolated service-level validation. This document does **not** describe
a separate phase-5 production runner.

The worker can use a verified reference recording. The smoke entrypoint
`scripts/smoke/run_fish_speech_smoke.py` checks the reference object and
SHA-256 before submitting paid inference. The controlled smoke helper is
`scripts/smoke/run_fish_speech_smoke_controlled.ps1` when an isolated test
is required. Never start a manually controlled worker when the global
Capacity Controller owns that service.

The service definition and immutable image configuration belong to
`deploy/salad/services.json`. For normal production use the managed
`run.ps1` workflow and the avatar's saved voice provider settings.
Service status, preparation and explicitly controlled shutdown use
`scripts/salad/manage_salad_worker.ps1`. The independent worker and its
smoke command are not an alternate narration orchestrator.

Review the intended Fish Speech licensing before deploying.
