# Salad startup performance

Cold-start latency is treated as a sequence of observable stages: allocation, image pull, model
download, runtime preparation, queue dispatch and inference.

## Prewarm

`scripts/salad/start_salad_optimized_prewarm.ps1` temporarily requests one replica while preserving
queue autoscaling at `min_replicas=0`. It applies bounded allocation/startup checks and may reallocate
an unhealthy or unproductive node.

Controlled pipeline wrappers prewarm only when cache inspection proves GPU work is still required.
They restore scale-to-zero and stop services during cleanup.

## Model download watchdog

Hugging Face-backed workers use
`ai_video_factory.workers.download_watchdog` to supervise downloads. The shared watchdog tracks
filesystem/process write progress and enforces bounded stall/hard-timeout policies. Services may also
request node reallocation for sustained poor transfer performance.

Breeze TTS 2, Fish Speech, Ideogram 4, Qwen Image 2.1, LTX 2.5 and Whisper reuse this shared mechanism.
Service-specific thresholds remain deployment settings rather than separate downloader implementations.

Real-ESRGAN ships its model artifact through its image/bootstrap path and does not require the Hugging
Face watchdog.

## Queue timing

Controlled production flows wait for the selected worker to become ready before charging normal job
dispatch time. Queue pending time should therefore represent dispatch/inference pressure rather than
model cold start.

LTX may scale horizontally after initial prewarm according to the replica ceiling in
`deploy/salad/services.json`.

## Tuning rule

Do not optimize cold start by weakening model integrity checks or silently extending timeouts. Change
thresholds only from measured logs, and validate that cleanup still returns the service to zero
replicas.
