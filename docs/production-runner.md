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
renderer only after phases 2-8 have completed successfully. If the orchestrator stops at a gate or a
stage fails, Phase 9 is not launched.

The source script must live below `data/` because that directory is the persistent host mount shared
with the task containers.

## Stage graph

The current execution order is:

```text
phase2-narrative
  -> phase3-continuity
  -> phase3-shots
  -> phase4-reference-prompts
  -> phase4-reference-assets
  -> phase5-narration
  -> phase5-alignment
  -> phase5-beat-timing
  -> phase5-shot-timing
  -> phase6-storyboard
  -> phase8-video-prompts
  -> phase6-keyframes       [manual production gate]
  -> phase8-videos
  -> local renderer         [PowerShell Compose handoff]
```

`phase8-video-prompts` intentionally runs before the keyframe gate because motion prompts depend on
the provider-neutral storyboard plan, not on generated keyframe pixels. This maximizes the amount of
work that can be completed before choosing the production keyframe model.

Storyboard grids are a diagnostic/editorial artifact and are not part of the minimum production
path to `FinalVideo`.

## Resume model

Operational state is stored in:

```text
data/output/production_manifest.json
```

For every completed stage the manifest stores:

- stage identity and kind;
- SHA-256 of the stage specification and executor script;
- SHA-256 over all declared input artifacts;
- SHA-256 over all declared output artifacts;
- whether the stage was executed by the runner or adopted from an existing run;
- the exact Python command used for automatic stages.

Domain JSON, PNG, WAV and MP4 files remain the source of production content. The production manifest
only decides whether a stage is current enough to skip.

The first time the runner sees a complete set of pre-existing outputs without a manifest record it
**adopts** those artifacts. This allows validated historical runs to enter the new orchestration model
without regenerating expensive media. Subsequent runs compare recorded fingerprints:

```text
same inputs + same outputs + same executor script -> SKIP
missing outputs                              -> RUN
changed inputs                               -> RUN
changed automatic-stage executor             -> RUN
changed automatic-stage outputs              -> RUN
```

Phase 8 retains its own `video_generation_manifest.json`. That manifest owns transport IDs, retry
counts and worker responses. `production_manifest.json` does not duplicate Salad transport state.

## Keyframe production gate

The production runner deliberately does **not** call `scripts/run_phase6_keyframes.py` automatically.
The production keyframe model has not been selected yet, and choosing it is an explicit architecture
decision.

The gate declares these upstream dependencies:

```text
storyboard_frames.json
shots.json
reference_assets.json
reference_assets/
```

and requires both:

```text
storyboard_keyframes.json
storyboard_keyframes/
```

If those artifacts already exist and match the current upstream fingerprints, they can be adopted as
supplied artifacts. For a new or stale storyboard the runner stops at `phase6-keyframes` with exit
code `20`. All completed upstream stages remain persisted and are skipped when execution resumes.

No production keyframe model is implied by this behavior.

## Inspection and targeted reruns

Show current state without changing the manifest or executing anything:

```bash
python scripts/run_production.py data/input/script.txt --plan
```

Stop after a specific stage:

```bash
python scripts/run_production.py data/input/script.txt \
  --through phase8-video-prompts
```

Force one automatic stage even if its fingerprints are current:

```bash
python scripts/run_production.py data/input/script.txt \
  --force-stage phase5-narration
```

The same force option is available through the PowerShell control plane:

```powershell
.\scripts\manage_local_stack.ps1 `
  -Action Production `
  -ScriptFile data\input\script.txt `
  -ForceStage phase5-narration
```

Forcing a manual gate is intentionally ineffective: manual/keyframe stages are never executed by the
runner.

## Expected exit behavior

- `0`: requested automatic stages completed, were adopted, or were already current.
- `20`: a manual production gate requires an artifact/architecture decision.
- `21`: a stage is blocked because required upstream inputs are missing.
- other non-zero codes: an underlying phase script or subprocess failed.

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
