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

## Narrative and visual hierarchy

```text
SourceScript
  -> NarrativeBlock[]
      -> Beat[]
          -> Scene[]
              -> Shot[]

ContinuityEntity[]
  -> VisualReference[]
```

Definitions:

- **NarrativeBlock**: contiguous semantic section of the script.
- **Beat**: one visualizable action, change or idea belonging to one block.
- **Scene**: ordered grouping of beat IDs.
- **Shot**: minimal audiovisual unit planned from consecutive beats inside one scene.
- **VisualReference**: provider-neutral canonical prompt bound to one continuity entity ID.

The planning contracts remain intentionally small:

```text
NarrativeBlock   = { id, text }
Beat             = { id, block_id, action }
Scene            = { id, beat_ids }
ContinuityEntity = { id, kind, name, description }
BlockContinuity  = { block_id, entity_ids }
Shot             = { id, scene_id, beat_ids, entity_ids, action }
VisualReference  = { entity_id, prompt }
```

Camera, lighting, duration, transitions and per-shot generation prompts remain outside these
contracts.

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

### Parallel fan-out / fan-in: beat extraction

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

For each scene, Python resolves the canonical beats and the union of continuity entities available
to the blocks represented by those beats. The model groups consecutive beats into shots and Python
validates exact beat coverage, ordering and entity references.

Shot IDs are assigned globally and deterministically by Python after each model response.

### Parallel fan-out / fan-in: visual references

Once continuity identities are resolved, each entity can be designed independently:

```text
entity 1 -> VisualReferenceBot --\
entity 2 -> VisualReferenceBot ----> VisualReference[]
entity N -> VisualReferenceBot --/
```

This stage is stateless and uses `asyncio.gather`. The workflow preserves input order and validates
that exactly one reference is returned for every canonical `entity_id`.

## Phase 4 visual reference boundary

`VisualReferenceBot` does **not** generate an entire image prompt freely. The model returns only a
stable English visual description for one entity. Python then applies a fixed template selected by
entity kind.

This division keeps high-level visual reasoning in the model while application-owned rules control
the reference format. Current templates cover:

```text
character -> neutral full-body identity reference
group     -> representative shared-appearance reference
location  -> permanent architecture/material/layout reference
object    -> isolated tangible-object reference
```

All templates explicitly avoid text, labels, watermarks and temporary shot-specific action. A
shared `visual_style` is supplied to the bot at runtime rather than added to the continuity schema.

The final Phase 4 contract is intentionally provider-neutral:

```text
VisualReference = { entity_id, prompt }
```

No image URL, file path, seed, model name or provider-specific parameter belongs in this contract.
Those fields become relevant only when an image-generation provider is introduced.

The bot may concretize moderate visual details when the narrative identity is underspecified because
the purpose of this stage is to lock a reusable appearance once. It must not add narrative facts,
relationships or unsupported written symbols.

## Provider boundaries

The original `StructuredTextProvider` remains unchanged for independent one-shot transformations.
Phase 3 adds `StatefulStructuredTextProvider`, whose result contains:

```text
StatefulStructuredResult
  output       -> validated Pydantic Structured Output
  response_id  -> provider state identifier for the next turn
```

`OpenAIProvider` implements both contracts. Phase 4 reference design returns to the stateless
`StructuredTextProvider` because canonical entities are independent inputs.

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

Phase 4 in progress:

6. `VisualReferenceBot`: parallel `ContinuityEntity[] -> VisualReference[]`

Real Phase 3 validation covered both stateful patterns. A three-block continuity example retained
stable entity IDs across turns. The samurai production example generated 7 shots from 3 scenes while
covering beats 1–11 exactly once and in order.

## Next Phase 4 increment

Validate `visual_references.json` with a real OpenAI run. After the reference prompts are judged
stable and useful, introduce an image-generation provider abstraction and persist generated
reference assets separately from the provider-neutral `VisualReference` contract.

Storyboard grids remain optional. They should be introduced only after single-entity reference
assets are working and only if they improve downstream video consistency enough to justify the
extra generation step.
