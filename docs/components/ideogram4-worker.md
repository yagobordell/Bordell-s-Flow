# Ideogram 4 worker

Ideogram 4 remains implemented and deployable, but it is **not** part of the normal production image
route. Qwen Image 2.1 generates Phase 4 references and Phase 6 keyframes.

Ideogram must never activate as a silent fallback.

## Preserved boundary

The dedicated worker keeps the open-weight Ideogram runtime, model bootstrap, structured caption
handling, deterministic job identity, safety handling and Salad deployment definition needed for
explicit testing or future reactivation.

Its local runtime consumes text conditioning. Production continuity must therefore be encoded in the
structured prompt rather than assuming binary reference-image conditioning.

The worker uses a local validated model snapshot and the shared inference core for transport, storage,
leases and replay. Bootstrap downloads are supervised by the shared download watchdog.

## Deployment

The service entry is `ideogram4` in `deploy/salad/services.json`. Mutable hardware, autoscaling,
queue and image values belong there.

Direct service management:

```powershell
pwsh scripts/salad/manage_salad_worker.ps1 -Service ideogram4 -Action Prepare
pwsh scripts/salad/manage_salad_worker.ps1 -Service ideogram4 -Action Status
```

Any future production reactivation should be explicit in workflow code, tests and documentation rather
than introduced as an automatic fallback.
