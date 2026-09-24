# Breeze TTS 2 worker

Breeze TTS 2 is the primary Phase 5 narration worker. It runs as a dedicated Salad service and
produces the canonical narration WAV used by alignment and composition.

## Runtime contract

The worker keeps a prepared Breeze runtime resident on GPU and processes one narration job at a time.
Long text is split at safe sentence boundaries when required by model/runtime limits, then reassembled
into one canonical mono WAV.

The provider-neutral `voice` field is interpreted as a speaker description; delivery instructions
remain separate. Numeric speech-speed requests are applied after synthesis with FFmpeg when needed.

Application identity fingerprints the narration-defining inputs so identical work can replay through
the shared Postgres/R2 idempotency path.

## Deployment

The canonical service definition is `breeze_tts2` in `deploy/salad/services.json`. That manifest,
not this document, owns image tags, GPU class, priority, replicas and runtime environment.

Prepare or inspect the service with:

```powershell
.\scripts\salad\manage_salad_worker.ps1 -Service breeze_tts2 -Action Prepare
.\scripts\salad\manage_salad_worker.ps1 -Service breeze_tts2 -Action Status
```

Model weights are bootstrapped into the worker model directory and supervised by the shared download
watchdog.

## Fallback

Fish Speech is a separate fallback service. It is started only for explicitly eligible terminal
Breeze failures; a successful Breeze run does not allocate Fish Speech.

See [Fish Speech fallback](fish-speech-fallback.md).

## License

The Breeze runtime source is Apache-2.0, while the model uses the BreezeBlue research/non-commercial
license. Any commercial deployment must re-evaluate that model license.
