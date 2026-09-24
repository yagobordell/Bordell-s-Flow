# Fish Speech fallback

Fish Speech S2 Pro is the self-hosted Phase 5 fallback for narration. Breeze TTS 2 remains the primary
provider.

```text
script
  -> Breeze
       | success -> narration.wav
       |
       +-- eligible terminal failure
            -> stop/verify Breeze
            -> Fish Speech
            -> narration.wav
```

## Policy

Fallback is explicit and conservative. Ordinary transient/control-plane problems should be retried or
reported rather than silently changing the voice backend.

The controlled narration workflow owns the eligibility classification, releases Breeze before Fish
capacity is started and preserves the same canonical narration contract for downstream stages.

Fish runs as its own Salad container group and image. Jobs remain in Postgres; mutable deployment
values are defined in `deploy/salad/services.json`.

## Voice and output

Fish generation may use a supplied reference asset to preserve the intended speaker identity. The
worker normalizes output into the same canonical WAV contract expected by Whisper and Phase 9.

Deterministic application identity includes the source text and generation-defining voice/model
inputs, allowing replay through the shared inference core.

## Operation

Normal production uses:

```text
scripts/pipeline/run_phase5_audio_controlled.ps1
```

Targeted paid validation uses the Fish-specific command under `scripts/smoke/`. Prepare/status/stop
operations use `scripts/salad/manage_salad_worker.ps1`.

Historical smoke timings, transport IDs and artifact hashes are intentionally kept in Git history
rather than this document.

## License

Fish Speech model/runtime licensing must be reviewed for the intended deployment. Do not infer
commercial rights from the fact that the service is self-hosted.
