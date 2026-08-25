# Production Architecture

## Core decision

The production pipeline starts from a **finished script**. Script generation is optional and
lives outside the core production flow.

```text
Optional topic -> ScriptWriterAgent -> generated Script
                                  \
                                   -> SourceScript -> production pipeline
User-written script ---------------/
```

`SourceScript` is deliberately minimal and contains only the script text.

## Bots before agents

Production planning uses bounded bots: one system prompt, one well-defined task and one
Structured Output contract. They do not decide what tool to call or autonomously change the
workflow.

Autonomous agents are reserved for later verification stages, where a verifier can inspect an
artifact and decide whether a specific step must be regenerated.

## Narrative hierarchy

```text
SourceScript
  -> NarrativeBlock[]
      -> Beat[]
          -> Scene[]
              -> Shot[]   (future step in Phase 3)
```

Definitions:

- **NarrativeBlock**: contiguous semantic section of the script.
- **Beat**: one visualizable action, change or idea belonging to one block.
- **Scene**: ordered grouping of beat IDs.
- **Shot**: future audiovisual unit generated from a scene.

The early contracts remain intentionally small:

```text
NarrativeBlock = { id, text }
Beat           = { id, block_id, action }
Scene          = { id, beat_ids }
```

Camera, lighting, transitions and image-generation prompts remain outside these narrative
contracts.

## Continuity registry

Phase 3 introduces a separate continuity layer instead of adding entity data to `NarrativeBlock`,
`Beat` or `Scene`.

```text
ContinuityEntity = { id, kind, name, description }
BlockContinuity  = { block_id, entity_ids }
```

Supported entity kinds are currently:

```text
character
group
location
object
```

The model never assigns canonical IDs. It only decides which known entities reappear and describes
new entities. Python assigns deterministic type-prefixed IDs such as `character_001` and
`location_001`.

This keeps identity stable even if model wording varies between calls.

## Orchestration patterns

### Parallel fan-out / fan-in

Independent narrative blocks are processed concurrently during beat extraction:

```text
block 1 -> BeatExtractorBot --\
block 2 -> BeatExtractorBot ----> Beat[] -> ScenePlannerBot
block N -> BeatExtractorBot --/
```

`asyncio.gather` performs the fan-out and the application restores deterministic global beat IDs
after the calls complete.

### Stateful serial continuity

Continuity-sensitive work runs in order:

```text
block 1 -> ContinuityBot -> response_id_1
                              |
                              v
block 2 -> ContinuityBot -> response_id_2
                              |
                              v
block 3 -> ContinuityBot -> ...
```

Each turn receives:

1. the current narrative block;
2. the canonical entity registry accumulated by Python;
3. the previous OpenAI response ID.

The OpenAI provider uses `previous_response_id` to continue the model context. Stable bot
instructions are sent again on every request instead of assuming that a previous response carries
them forward.

The application remains the source of truth for IDs, ordering and relationships. Provider-managed
conversation state is used only for semantic continuity.

## Provider boundaries

The original `StructuredTextProvider` remains unchanged for independent one-shot transformations.
Phase 3 adds `StatefulStructuredTextProvider`, whose result contains:

```text
StatefulStructuredResult
  output       -> validated Pydantic Structured Output
  response_id  -> provider state identifier for the next turn
```

`OpenAIProvider` implements both contracts. This allows Phase 1 and Phase 2 bots to remain stateless
while continuity and later shot planning can use serial state.

## Phase 1 compatibility

The Phase 1 `DirectorAgent` remains as an experiment and regression fixture. Its rich scene type
has been renamed to `StoryboardScene` so it cannot be confused with the new minimal `Scene`
contract.

The Phase 1 flow is not the long-term production architecture.

## Planned media stack

- **LLM orchestration:** OpenAI Responses API + Structured Outputs.
- **GPU inference:** Docker containers on Salad.
- **Video model:** LTX-2.5 first; benchmark hardware/quantization before fixing the worker shape.
- **Object storage:** Cloudflare R2.
- **Job/application state:** Supabase/Postgres.
- **Composition:** Remotion for timeline, transitions, captions and motion graphics.
- **Media plumbing:** FFmpeg/ffprobe for probing, codecs, audio, transcoding and muxing.
- **Model execution:** Python/PyTorch directly; no ComfyUI dependency.

## Current implementation status

Completed:

1. `NarrativeBlockBot`: `SourceScript -> NarrativeBlock[]`
2. `BeatExtractorBot`: `NarrativeBlock -> Beat[]` in parallel
3. `ScenePlannerBot`: `Beat[] -> Scene[]`
4. `ContinuityBot`: serial `NarrativeBlock[] -> ContinuityEntity[] + BlockContinuity[]`

Next Phase 3 increment:

```text
Scene[1] -> ShotPlannerBot -> state
Scene[2] -> ShotPlannerBot -> state
Scene[N] -> ShotPlannerBot -> Shot[]
```

The shot contract should remain minimal and will be designed only after the continuity output is
validated with a real multi-block OpenAI run.
