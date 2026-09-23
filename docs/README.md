# Documentation

Active documentation describes the current system only. Historical validation runs, migration notes,
old image digests and retired implementation handoffs belong in Git history.

## Architecture

- [System overview](architecture/overview.md)
- [Shared inference worker core](architecture/inference-worker-core.md)

## Components

- [Breeze TTS 2 worker](components/breeze-tts2-worker.md)
- [Fish Speech fallback](components/fish-speech-fallback.md)
- [Whisper worker](components/whisper-worker.md)
- [Ideogram 4 worker](components/ideogram4-worker.md)
- [LTX 2.5 worker](components/ltx25-worker.md)
- [LTX 2.5 audio-to-video mode](components/ltx25-a2v.md)
- [Phase 8 video generation](components/phase8-video-generation.md)
- [Phase 9 compositor](components/phase9-compositor.md)

## Operations

- [Production runner](operations/production-runner.md)
- [Salad stack deployment](operations/salad-stack-deployment.md)
- [Salad startup performance](operations/salad-startup-performance.md)
- [Deployment validation](operations/deployment-validation.md)

Mutable deployment values such as image tags, queues, GPU classes, replica ceilings and runtime
environment belong to `deploy/salad/services.json`.
