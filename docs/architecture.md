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
      -> ReferenceAsset[]
```

Definitions:

- **NarrativeBlock**: contiguous semantic section of the script.
- **Beat**: one visualizable action, change or idea belonging to one block.
- **Scene**: ordered grouping of beat IDs.
- **Shot**: minimal audiovisual unit planned from consecutive beats inside one scene.
- **VisualReference**: provider-neutral canonical prompt bound to one continuity entity ID.
- **ReferenceAsset**: persisted image URI bound to the same canonical entity ID.

The planning and reference contracts remain intentionally small:

```text
NarrativeBlock   = { id, text }
Beat             = { id, block_id, action }
Scene            = { id, beat_ids }
ContinuityEntity = { id, kind, name, description }
BlockContinuity  = { block_id, entity_ids }
Shot             = { id, scene_id, beat_ids, entity_ids, action }
VisualReference  = { entity_id, prompt }
ReferenceAsset   = { entity_id, uri }
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
entity 1 + context -> VisualReferenceBot --\
entity 2 + context -> VisualReferenceBot ----> VisualReference[]
entity N + context -> VisualReferenceBot --/
```

This stage is stateless and uses `asyncio.gather`. The workflow preserves input order and validates
that exactly one reference is returned for every canonical `entity_id`.

Narrative context is derived by Python from `NarrativeBlock[]` and `BlockContinuity[]`. Each entity
receives only the blocks where it is active. The context is temporary evidence for visual design;
it is not copied into the persisted `VisualReference` contract.

### Parallel fan-out / fan-in: reference assets

Validated prompts can then be rendered independently:

```text
VisualReference 1 -> ImageProvider --\
VisualReference 2 -> ImageProvider ----> GeneratedImage[] -> ReferenceAsset[]
VisualReference N -> ImageProvider --/
```

Provider calls run concurrently. Binary image payloads remain in memory until all generations have
succeeded. Only then does the application write deterministic files and persist `ReferenceAsset[]`.
This prevents a failed provider call from leaving a partially committed local batch.

## Phase 4 visual reference boundary

`VisualReferenceBot` does **not** generate an entire image prompt freely. The model returns only a
stable English visual description for one entity. Python then applies a fixed template selected by
entity kind.

Current templates cover:

```text
character -> neutral full-body identity reference
group     -> representative shared-appearance reference
location  -> single coherent environment reference
object    -> isolated tangible-object reference
```

All templates explicitly avoid text, labels, watermarks and temporary shot-specific action. A
shared `visual_style` is supplied at runtime rather than added to the continuity schema.

The model may concretize moderate visual details when the narrative identity is underspecified,
because this stage must lock a reusable appearance. It must not add unsupported narrative facts,
relationships or written symbols.

### Contextual location rule

Broad locations such as a country, city or region are not treated as encyclopedic descriptions or
multi-era montages. The bot receives the narrative context where the location is active and chooses
one reusable physical environment that matches that context.

This rule was added after a real validation where `Japan` initially drifted toward a contemporary
country description. With contextual input, the same canonical location produced a coherent
feudal-era Japanese castle-town environment instead.

## Image provider boundary

Image generation is separated from prompt design.

Provider-neutral contract:

```text
ImageProvider.generate_image(...)
  -> GeneratedImage
       content     # bytes, ephemeral
       media_type
       extension
```

The application domain does not persist provider response objects or base64 payloads.

The first implementation is `OpenAIImageProvider`. Model, size and quality are runtime concerns,
not fields of `ReferenceAsset`.

Persisted application contract:

```text
ReferenceAsset = { entity_id, uri }
```

The local implementation uses deterministic filenames such as:

```text
reference_assets/group_001.png
reference_assets/location_001.png
```

This keeps canonical identity stable while allowing local storage to be replaced by an object-store
URI later without changing the entity relationship.

## Provider boundaries

The original `StructuredTextProvider` remains for independent one-shot transformations. Phase 3
adds `StatefulStructuredTextProvider`, whose result contains:

```text
StatefulStructuredResult
  output       -> validated Pydantic Structured Output
  response_id  -> provider state identifier for the next turn
```

`OpenAIProvider` implements both text contracts. Stable instructions are sent on every stateful
request. The application remains the source of truth for IDs, ordering and relationships.

Phase 4 adds a separate `ImageProvider`. This is intentionally not folded into the text-provider
interface because image generation has a different return type, persistence lifecycle and runtime
configuration.

## Phase 4 artifacts

Prompt design:

```bash
python scripts/run_phase4.py
```

Produces:

```text
data/output/phase4/visual_references.json
```

Reference-image generation:

```bash
python scripts/run_phase4_assets.py --quality medium
```

Produces:

```text
data/output/phase4/
├── visual_references.json
├── reference_assets.json
└── reference_assets/
    ├── group_001.png
    ├── group_002.png
    └── location_001.png
```

## Real Phase 4 validation

The samurai production example validated both halves of Phase 4:

- 3 canonical continuity entities received stable visual prompts.
- `group_001` produced a reusable samurai group reference.
- `group_002` produced a visually differentiated feudal-lord reference.
- `location_001` produced one coherent feudal Japanese environment without unjustified modern drift.
- The 3 prompts generated 3 usable PNG files.
- `reference_assets.json` preserved all 3 canonical IDs with deterministic relative URIs.
- Local `pytest` and Ruff validation passed before the real generation run.

These results are sufficient to close Phase 4. Storyboard grids remain an optional experiment and
are not required by the canonical reference contract.

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
6. `VisualReferenceBot`: contextual parallel `ContinuityEntity[] -> VisualReference[]`
7. `ImageProvider` + `OpenAIImageProvider`: `VisualReference[] -> GeneratedImage[]`
8. Reference asset workflow: `GeneratedImage[] -> ReferenceAsset[] + PNG files`

Real Phase 3 validation covered both stateful patterns. Real Phase 4 validation covered contextual
prompt design and provider-backed reference-image generation.

## Next implementation step

**Phase 5 — Audio and timing.**

The next formal stage should introduce TTS, measured audio durations and alignment/caption timing
without expanding the already-closed visual-reference contracts.

Storyboard grids remain optional. They should only be introduced if a later benchmark shows that
they improve downstream video consistency enough to justify the extra generation step.
