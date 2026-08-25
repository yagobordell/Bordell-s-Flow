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
              -> Shot[]
```

Definitions:

- **NarrativeBlock**: contiguous semantic section of the script.
- **Beat**: one visualizable action, change or idea belonging to one block.
- **Scene**: ordered grouping of beat IDs.
- **Shot**: minimal audiovisual unit planned from consecutive beats inside one scene.

The planning contracts remain intentionally small:

```text
NarrativeBlock   = { id, text }
Beat             = { id, block_id, action }
Scene            = { id, beat_ids }
ContinuityEntity = { id, kind, name, description }
BlockContinuity  = { block_id, entity_ids }
Shot             = { id, scene_id, beat_ids, entity_ids, action }
```

Camera, lighting, duration, transitions and generation prompts remain outside these contracts.

## Continuity registry

Phase 3 adds a separate continuity layer instead of adding entity data directly to
`NarrativeBlock`, `Beat` or `Scene`.

Supported entity kinds are:

```text
character
group
location
object
```

`object` means a physical tangible object. Abstract concepts, values, doctrines or mental states
are not continuity entities. They can remain in narration and shot actions without receiving an
entity ID.

The model never assigns canonical IDs. It only decides which known entities reappear and describes
new entities. Python assigns deterministic type-prefixed IDs such as `character_001` and
`location_001`.

Entities can remain contextually active across blocks when the narrative clearly continues in the
same place or situation even if the next block does not repeat their names. They are not retained
merely because they appeared earlier.

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

Each turn receives the current block, the canonical entity registry accumulated by Python and the
previous provider response ID.

### Stateful serial shot planning

Shot planning uses a **separate** state chain:

```text
scene 1 -> ShotPlannerBot -> shot_response_id_1
                              |
                              v
scene 2 -> ShotPlannerBot -> shot_response_id_2
                              |
                              v
scene N -> ShotPlannerBot -> Shot[]
```

The continuity state chain is not reused by the shot planner. Canonical continuity moves between
stages through application-owned entity IDs and persisted artifacts. Provider-managed state is used
only for semantic context within one bot's serial process.

For each scene, Python resolves:

1. the scene's canonical beat objects;
2. the narrative blocks referenced by those beats;
3. the union of continuity entities available to those blocks.

The model then groups consecutive beats into visual shots, selects only relevant canonical entity
IDs and describes the visual action. Python validates that all scene beats appear exactly once and
in order and that no unknown entity is referenced.

Shot IDs are assigned globally and deterministically by Python after each model response.

## Provider boundaries

The original `StructuredTextProvider` remains unchanged for independent one-shot transformations.
Phase 3 adds `StatefulStructuredTextProvider`, whose result contains:

```text
StatefulStructuredResult
  output       -> validated Pydantic Structured Output
  response_id  -> provider state identifier for the next turn
```

`OpenAIProvider` implements both contracts. This lets Phase 1 and Phase 2 remain stateless while
continuity and shot planning use serial state.

Stable instructions are sent on every stateful request. The application remains the source of
truth for IDs, ordering and relationships.

## Phase 1 compatibility

The Phase 1 `DirectorAgent` remains as an experiment and regression fixture. Its rich scene type
has been renamed to `StoryboardScene` so it cannot be confused with the production `Scene`
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
5. `ShotPlannerBot`: serial `Scene[] -> Shot[]`

Real validation covered both stateful patterns. A three-block continuity example retained stable
entity IDs across turns. The samurai production example generated 7 shots from 3 scenes while
covering beats 1–11 exactly once and in order.

## Next implementation step

Phase 4 adds **visual references** on top of canonical continuity IDs rather than changing the
narrative contracts.

Likely first increment:

```text
ContinuityEntity[]
    -> CharacterReferenceBot / LocationReferenceBot
    -> reference prompts / reference assets
```

Character and location reference contracts should remain minimal. Camera, per-shot generation
prompts and storyboard-grid strategy should be added only when required by the following stage.
