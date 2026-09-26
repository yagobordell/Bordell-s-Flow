# Retirement audit: planning bots and Salad services

## Decision and scope

The active planning interface is B1.1 → B1.2 → B2. The old planning bot
package was removed, along with its seven OpenAI-powered phase entrypoints,
their seven bot-calling workflows, and the deprecated video-wide production
runner and its DAG. Tests that exclusively exercised the retired code were
removed; B1/B2 and the independently deployed GPU clients retain their tests.
No original planning bot is retained as a hidden fallback.

**Deliberate functional boundary:** the B runner currently ends at the
validated `visual_plan.json`. It does not render a complete video. The
pre-existing GPU stage clients can still run against their explicit old
artifacts for service validation and cache/replay diagnostics; those clients
do not yet accept B2's schema.

## Why the old production DAG could not stay

The retired phase 2 emitted numeric beat IDs and scene groupings. The old
continuity and shot planners produced integer shot IDs. Timing, storyboard
keyframes, LTX clip creation and Remotion composition then joined their
artifacts by those shot IDs. B1.2 freezes string beat IDs such as `1A`,
while B2 attaches one of `avatar`, `avatar_media`, `media_image`, or
`media_video` to each beat. B2 has no old scene/shot contract.

Converting B2 to synthetic old shots would lose or invent required visual
and timeline information. Removing the old bots but leaving the old DAG
would leave broken imports and missing phase scripts. The correct boundary
was to retire that unsupported orchestrator, without touching service
transport or model runtimes.

## Preserved Salad and storage dependencies

The manifest `deploy/salad/services.json` still defines all seven services:
`breeze_tts2`, `fish_speech`, `whisper`, `ideogram4` (optional),
`qwen_image_21`, `ltx25` and `realesrgan`. Every model-specific runtime
and Dockerfile is retained.

- `src/ai_video_factory/inference/` retains worker claims, leases,
  heartbeats, health, capacity reconciliation and recovery.
- `src/ai_video_factory/providers/postgres_queue.py`,
  `inference_jobs.py` and `r2.py` retain durable job submission and
  object storage.
- `scripts/salad/` retains the stack/worker managers and global
  Capacity Controller.
- `scripts/smoke/`, `scripts/diagnostics/`, and the controlled per-stage
  GPU wrappers remain in place. Their existing tests continue to run.
- `src/ai_video_factory/providers/openai.py` retains ordinary and metered
  Structured Outputs; only the retired stateful-continuation API for the old
  planning bots was removed.

This deletion does not modify the Salad manifest, any worker runtime or
Dockerfile, Postgres migrations, R2 storage or the inference/queue/capacity
implementations. It also does not redeploy or stop any running Salad service.

## Remaining downstream work

A future end-to-end runner must define and validate the mapping from each
frozen B2 beat and visual strategy into source-preserving narration timing,
avatar/media generation, Qwen references and keyframes, LTX conditioning,
upscaling and the Remotion timeline. It must preserve every string beat ID
and B2 decision, incorporate per-service cache/replay and queue authority,
and protect the shared Capacity Controller across concurrent runs.
Its tests must cover all four visual types, alignment and render continuity,
service failures and recovery, and end-to-end quality; passing unit tests
alone is not a claim of completed live GPU integration.
