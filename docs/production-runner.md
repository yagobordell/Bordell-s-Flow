# Production runner

The production runner turns the validated phase scripts into one resumable execution plan without
moving their business logic into a second implementation.

## Command surface

Inside the local orchestrator container:

```bash
python scripts/run_production.py data/input/script.txt
```

From Windows PowerShell, the local Compose control plane exposes the complete handoff:

```powershell
.\scripts\manage_local_stack.ps1 -Action Production -ScriptFile data\input\script.txt
```

The `Production` action runs the Python orchestrator first and invokes the isolated Remotion/FFmpeg
renderer only after phases 2-8 have completed successfully. If a stage fails, Phase 9 is not launched.

The source script must live below `data/` because that directory is the persistent host mount shared
with the task containers.

## Stage graph

The execution order is:

```text
phase2-narrative
  -> phase3-continuity
  -> phase3-shots
  -> phase4-reference-prompts
  -> phase4-reference-assets       [Ideogram 4 Quality]
  -> phase5-narration              [Breeze TTS 2 -> Fish Speech fallback]
  -> phase5-alignment              [Whisper]
  -> phase5-beat-timing
  -> phase5-shot-timing
  -> phase6-storyboard
  -> phase8-video-prompts
  -> phase6-keyframes              [Ideogram 4 Quality]
  -> phase8-videos                 [LTX 2.5]
  -> local renderer                [Remotion + FFmpeg]
```

`phase8-video-prompts` intentionally runs before keyframe generation because motion prompts depend on
the provider-neutral storyboard plan, not on generated keyframe pixels. This allows the semantic
motion plan and the image generation stage to remain independently resumable.

Storyboard grids are a diagnostic/editorial artifact and are not part of the minimum production
path to `FinalVideo`.

## Ideogram keyframes

`phase6-keyframes` is an automatic production stage and invokes:

```text
scripts/run_phase6_keyframes.py
```

The stage uses the dedicated Salad Ideogram worker already shared with Phase 4 reference generation.
Its production parameters are explicit in the stage specification:

```text
model   = ideogram-ai/ideogram-4-nf4 (worker/config default)
quality = high -> V4_QUALITY_48 in the worker
size    = 1024x1536
queue   = SALAD_IDEOGRAM4_QUEUE_NAME
```

The open-weight Ideogram pipeline is text-conditioned. Therefore the keyframe stage fingerprints and
passes only the data it actually consumes:

```text
storyboard_frames.json
shots.json
```

The canonical visual continuity information has already been encoded by the storyboard planning step
into the structured Ideogram caption. `reference_assets.json` and its PNG files remain persisted
review/evidence artifacts, but they are not declared as binary conditioning inputs to keyframe
inference. Consequently, changing a Phase 4 PNG without changing the structured storyboard caption
does not unnecessarily invalidate every keyframe.

Generation fans out through the shared Ideogram Job Queue. Parallelism comes from independent Salad
container replicas; each replica keeps one Ideogram runtime resident on its RTX 4090.

## Phase 5 speech fallback

Phase 5 keeps Breeze TTS 2 as the primary provider. The controlled PowerShell wrapper starts
Fish Speech only after an explicitly classified terminal Breeze failure and only after Breeze
has been stopped and verified at zero replicas. A successful Breeze run therefore consumes zero
Fish GPU-seconds.

Fish uses its own queue and container group, plus one persistent authorized reference voice in R2.
The `FISH_SPEECH_REFERENCE_*` values are deployment configuration, not per-video inputs. Before
Fish GPU allocation, the wrapper validates the reference configuration, verifies the R2 object and
checks its SHA-256. Fish output is normalized to the same PCM16 mono 24 kHz WAV contract consumed by
Whisper.

The fallback implementation and real Salad validation evidence are documented in
`docs/fish-speech-fallback.md`.

## Resume model

Operational state is stored in:

```text
data/output/production_manifest.json
```

The current manifest schema is `2`. For every completed stage it stores:

- stage identity;
- SHA-256 of the stage specification and executor script;
- SHA-256 over all declared input artifacts;
- SHA-256 over all declared output artifacts;
- whether the stage was executed by the runner or adopted from an existing run;
- the exact Python command used.

Domain JSON, PNG, WAV and MP4 files remain the source of production content. The production manifest
only decides whether a stage is current enough to skip.

The first time the runner sees a complete set of pre-existing outputs without a manifest record it
**adopts** those artifacts. This allows validated historical runs to enter the orchestration model
without regenerating expensive media. Subsequent runs compare recorded fingerprints:

```text
same inputs + same outputs + same executor script -> SKIP
missing outputs                                  -> RUN
changed inputs                                   -> RUN
changed executor                                 -> RUN
changed outputs                                  -> RUN
```

A schema-v1 manifest created while keyframes were a manual gate is upgraded on read. Automatic stage
records are preserved; the old manual `phase6-keyframes` record is discarded so the new Ideogram
stage can be adopted from complete existing artifacts or executed normally.

Phase 8 retains its own `video_generation_manifest.json`. That manifest owns transport IDs, retry
counts and worker responses. `production_manifest.json` does not duplicate Salad transport state.

## Inspection and targeted reruns

Show current state without changing the manifest or executing anything:

```bash
python scripts/run_production.py data/input/script.txt --plan
```

Stop after a specific stage:

```bash
python scripts/run_production.py data/input/script.txt \
  --through phase6-keyframes
```

Force one stage even if its fingerprints are current:

```bash
python scripts/run_production.py data/input/script.txt \
  --force-stage phase6-keyframes
```

The same force option is available through the PowerShell control plane:

```powershell
.\scripts\manage_local_stack.ps1 `
  -Action Production `
  -ScriptFile data\input\script.txt `
  -ForceStage phase6-keyframes
```

## Expected exit behavior

- `0`: requested stages completed, were adopted, or were already current.
- `21`: a stage is blocked because required upstream inputs are missing.
- other non-zero codes: an underlying phase script or subprocess failed.

There is no keyframe architecture gate in the production path anymore: Ideogram 4 Quality is the
selected production model for both Phase 4 references and Phase 6 keyframes.

## Local/cloud boundary

The runner preserves the architecture boundary introduced by the local Compose control plane:

```text
orchestrator container
  Python control flow
  OpenAI semantic planning
  Salad queue clients
  R2/Postgres clients
         |
         v
remote model-specific Salad workers
  Whisper       -> RTX 3090
  Breeze TTS 2  -> RTX 4090, Phase 5 primary
  Fish Speech   -> RTX 4090, Phase 5 fallback only
  Ideogram 4    -> RTX 4090, shared Phase 4 + Phase 6 queue
  LTX 2.5       -> RTX 5090
         |
         v
persisted ./data
         |
         v
renderer container
  Python + Remotion + Chrome + FFmpeg
```

The orchestrator does not gain Node, Chrome, FFmpeg, CUDA or model weights, and the renderer does not
own cloud inference orchestration.
