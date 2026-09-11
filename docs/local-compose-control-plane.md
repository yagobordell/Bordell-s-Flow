# Local Docker control plane

The local machine owns orchestration and final composition. Heavy model inference remains remote in
model-specific Salad container groups.

```text
Docker Desktop / local host
        |
        +--> orchestrator container (Python 3.12)
        |      +--> OpenAI semantic planning
        |      +--> Salad Job Queues
        |      +--> R2 + Postgres
        |      `--> data/ persisted on the host
        |
        `--> renderer container (Python 3.12 + Node 22 + Remotion + FFmpeg)
               +--> reads Phase 5/8 contracts from data/
               +--> stages clips for Remotion
               +--> renders the silent Phase 9 visual
               `--> muxes narration into final_video.mp4
```

No GPU model is installed in either local image. Whisper, Breeze TTS 2, Ideogram 4 Quality and LTX
2.5 continue to execute in Salad and scale independently.

## Images

`compose.yaml` defines two one-shot task services:

- `orchestrator`: the Python application, providers, workflows and phase scripts;
- `renderer`: the same Python contracts plus Node 22, Remotion, Chrome Headless Shell and system
  FFmpeg/ffprobe.

Both services mount the repository `data/` directory at `/workspace/data`. The Compose environment
overrides `OUTPUT_DIR` and `TEMP_DIR` to absolute container paths under that mount, so persisted
artifacts are shared across one-shot containers and survive container removal.

The renderer follows the current Remotion Docker guidance: Debian, Node 22, the documented Chrome
shared libraries and `npx remotion browser ensure` during image build. System FFmpeg is also installed
because Phase 9.5 intentionally performs the final narration mux through the Python compositor.

## Validate and build

From the repository root:

```powershell
pwsh scripts/manage_local_stack.ps1 -Action Validate
pwsh scripts/manage_local_stack.ps1 -Action Build
pwsh scripts/manage_local_stack.ps1 -Action Smoke
```

`Validate` only checks Docker/Compose and the Compose model. `Build` creates the two local images.
`Smoke` runs short checks inside both images and verifies that the renderer contains Node, npm/npx,
FFmpeg, ffprobe and the installed Remotion CLI.

The local manager never starts or modifies Salad resources.

## Run Python phases

Any existing phase script can run inside the orchestrator image by overriding the default Compose
command. For example:

```powershell
docker compose run --rm orchestrator python scripts/run_phase4_assets.py
docker compose run --rm orchestrator python scripts/run_phase5_audio.py
docker compose run --rm orchestrator python scripts/run_phase5_alignment.py
docker compose run --rm orchestrator python scripts/run_phase6_keyframes.py
docker compose run --rm orchestrator python scripts/run_phase8_videos.py
```

The services load `.env` when it exists. API-backed phases still require the same OpenAI, Salad, R2
and Postgres values as the host scripts. The image does not bake `.env` or secrets into Docker layers.

## Render Phase 9

When Phase 5 timing/audio and Phase 8 clips have been persisted, the renderer can finish Phase 9 with
one command:

```powershell
pwsh scripts/manage_local_stack.ps1 -Action Phase9
```

That is equivalent to:

```powershell
docker compose run --rm renderer
```

The image runs, in order:

```text
run_phase9_compositor.py
        -> composition_plan.json
run_phase9_motion.py
        -> visual_motion.mp4
run_phase9_final.py
        -> final_video.mp4 + final_video.json
```

The container is removed afterwards, while all final outputs remain below `data/output/phase9` on the
host.

For debugging, any renderer command can also be invoked explicitly:

```powershell
docker compose run --rm renderer python scripts/run_phase9_motion.py --prepare-only
docker compose run --rm renderer python scripts/run_phase9_final.py --prepare-only
```

## Relationship with the Salad stack

The local and remote managers have intentionally separate responsibilities:

```text
scripts/manage_salad_stack.ps1  -> provision/start/status/stop remote model groups
scripts/manage_local_stack.ps1  -> validate/build/smoke/run local task containers
```

Starting the local Compose task containers does not start paid GPUs. Starting Salad groups also keeps
`min_replicas=0`, so GPU replicas are created only when jobs are queued.

## Reproducibility boundary

The Python images use Python 3.12 on Debian Bookworm. The renderer copies Node 22 from the official
Node Bookworm image and uses the exact Remotion package versions already pinned in
`remotion/package.json`. Model weights and GPU runtimes remain outside these images by design.

`data/` is excluded from Docker build contexts and only bind-mounted at runtime, preventing generated
media or credentials from being copied into local images.
