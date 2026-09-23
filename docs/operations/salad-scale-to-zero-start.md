# Salad scale-to-zero Start semantics

All production inference services use queue autoscaling with `min_replicas=0` so idle GPU workers cost nothing.

For this configuration, the Salad Container Group can remain in `deploying` while `replicas=0` and `pending_change=false`. That state is accepted as an active scale-to-zero group: no container instance exists yet, and the first queued job may trigger the cold start.

The deployment-validation command therefore treats either of these states as a successful `Start`:

- `running` with no pending change; or
- `deploying`, `replicas=0`, no pending change, and `min_replicas=0`.

It still fails immediately if Salad reports `failed`, and it uses a short activation timeout for any other transitional state.

Use the validation entry point rather than waiting manually for an idle group to become `running`:

```powershell
.\scripts\manage_salad_validation.ps1 -Service breeze_tts2 -Action Start
```

After `Start` succeeds, submit the smoke job. The smoke is the operation expected to make the autoscaler allocate a GPU replica and exercise the worker startup/readiness probes.
