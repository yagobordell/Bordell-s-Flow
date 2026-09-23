# Scripts

Operational executables are grouped by responsibility:

- `pipeline/`: production entry points, preflight, phase runners and shared R2 orchestration helpers.
- `salad/`: SaladCloud lifecycle, queue and deployment management.
- `smoke/`: explicit smoke and validation workloads.
- `diagnostics/`: cache audits, inspectors, queue diagnostics and metrics collection.

The normal end-to-end entry point is `pipeline/run_video_factory.ps1`.
