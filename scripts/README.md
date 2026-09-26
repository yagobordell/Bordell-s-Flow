# Scripts

Operational executables are grouped by responsibility:

- `pipeline/run_b_pipeline.py`: active B1.1 → B1.2 → B2 planning; no Salad
  capacity is requested by this runner.
- `pipeline/`: independently runnable GPU and composition stage clients,
  cache audits/preflights and controlled wrappers; no end-to-end video runner.
- `salad/`: SaladCloud lifecycle, Postgres-backed capacity control and deployment.
- `smoke/`: explicit inference smoke and validation workloads.
- `diagnostics/`: queue, cache and recovery inspectors.

The scene/shot planning bots and their one-command production orchestrator have
been retired; stage clients remain for their existing explicitly supplied
artifacts until a source-preserving downstream B2 migration is validated.
