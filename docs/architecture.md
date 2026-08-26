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

## Narrative, visual and temporal hierarchy

```text
SourceScript
  -> NarrativeBlock[]
      -> Beat[]
          -> Scene[]
              -> Shot[]

ContinuityEntity[]
  -> VisualReference[]
      -> ReferenceAsset[]

SourceScript.text
  -> NarrationAudio
      -> NarrationWord[]
          -> BeatTiming[]
              -> ShotTiming[]
```

Definitions:

- **NarrativeBlock**: contiguous semantic section of the script.
- **Beat**: one visualizable action, change or idea belonging to one block.
- **Scene**: ordered grouping of beat IDs.
- **Shot**: minimal audiovisual unit planned from consecutive beats inside one scene.
- **VisualReference**: provider-neutral canonical prompt bound to one continuity entity ID.
- **ReferenceAsset**: persisted image URI bound to the same canonical entity ID.
- **NarrationAudio**: canonical narration URI plus measured playback duration.
- **NarrationWord**: recognized word and timestamps used only as timing evidence.
- **BeatTiming**: canonical narration interval assigned to one beat.
- **ShotTiming**: deterministic projection of the beat timeline onto one existing shot.

The contracts remain intentionally small:

```text
NarrativeBlock   = { id, text }
Beat             = { id, block_id, action }
Scene            = { id, beat_ids }
ContinuityEntity = { id, kind, name, description }
BlockContinuity  = { block_id, entity_ids }
Shot             = { id, scene_id, beat_ids, entity_ids, action }
VisualReference  = { entity_id, prompt }
ReferenceAsset   = { entity_id, uri }
NarrationAudio   = { uri, duration_seconds }
NarrationWord    = { id, text, start_seconds, end_seconds }
BeatTiming       = { beat_id, start_word_id, end_word_id, start_seconds, end_seconds }
ShotTiming       = { shot_id, start_seconds, end_seconds }
```

Camera, lighting, transitions and per-shot generation prompts remain outside the closed narrative
contracts. Timing is stored in parallel artifacts instead of mutating `Beat` or `Shot`.

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

### Single canonical narration

Phase 5 deliberately generates one continuous narration instead of TTS per shot:

```text
SourceScript.text
      ↓
SpeechProvider
      ↓
GeneratedSpeech bytes
      ↓
validate WAV + measure real PCM duration
      ↓
NarrationAudio
```

This preserves prosody and prevents the still-evolving visual plan from forcing artificial audio
cuts. The workflow validates the WAV before persistence and measures duration from the PCM frames
that are actually present, rather than trusting a possibly streaming/sentinel data-size header.

### Word-level timing evidence

```text
SourceScript.text + narration.wav
              ↓
TranscriptionProvider
              ↓
NarrationWord[]
```

`NarrationWord.text` is not canonical narrative text. It is recognized evidence used to attach time
to the immutable source script. Small transcription artifacts are therefore preserved rather than
silently rewriting the source.

Timestamp validation allows point-like words where `start_seconds == end_seconds`, because real
Whisper output can quantize some words to a zero-duration point. The global sequence must still be
non-decreasing and remain inside the measured narration duration.

### Model-owned beat boundaries, Python-owned time

`BeatTimingBot` follows the same boundary pattern used earlier in narrative segmentation:

```text
SourceScript + Beat[] + NarrationWord[]
                 ↓
BeatTimingBot
                 ↓
beat_end_word_ids[]
                 ↓
Python reconstruction
                 ↓
BeatTiming[]
```

The model decides only which recognized word ends each ordered beat. It never generates timestamps.
Python reconstructs consecutive word ranges and the real timeline.

For non-final beats, the interval ends at the start of the next beat's first word. This assigns the
inter-beat pause to the previous visual state and makes the next visual change coincide with the
start of the next spoken idea. The final beat ends at `NarrationAudio.duration_seconds`.

### Deterministic shot timing

No model is required after beat timing:

```text
Shot.beat_ids + BeatTiming[]
            ↓
Python
            ↓
ShotTiming[]
```

A shot starts at its first beat's `start_seconds` and ends at its last beat's `end_seconds`. The
workflow validates exact beat coverage, ordering and contiguous timelines before persisting the
result.

This means the pipeline does not assume a fixed number of shots. A later regeneration can change
shot grouping while temporal projection remains deterministic as long as beat coverage is valid.

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

## Media provider boundaries

### Structured text

`StructuredTextProvider` handles independent Structured Output transformations. Phase 3 adds
`StatefulStructuredTextProvider`, whose result contains:

```text
StatefulStructuredResult
  output       -> validated Pydantic Structured Output
  response_id  -> provider state identifier for the next turn
```

`OpenAIProvider` implements both text contracts. Stable instructions are sent on every stateful
request. The application remains the source of truth for IDs, ordering and relationships.

### Images

Image generation is separated from prompt design:

```text
ImageProvider.generate_image(...)
  -> GeneratedImage
       content     # bytes, ephemeral
       media_type
       extension
```

The application domain does not persist provider response objects or base64 payloads. The first
implementation is `OpenAIImageProvider`. Model, size and quality are runtime concerns, not fields of
`ReferenceAsset`.

Persisted application contract:

```text
ReferenceAsset = { entity_id, uri }
```

### Speech

```text
SpeechProvider.generate_speech(...)
  -> GeneratedSpeech
       content     # bytes, ephemeral
       media_type
       extension
```

The first implementation is `OpenAISpeechProvider`. Voice, model, speed and speech instructions are
runtime generation parameters and are not duplicated in `NarrationAudio`.

### Transcription / alignment

`TranscriptionProvider` exposes word-level timing evidence independently of the speech provider.
The current OpenAI implementation uses the provider path that exposes word timestamps. The domain
stores only normalized `NarrationWord[]`, not raw provider response objects.

## Phase 4 artifacts

Prompt design:

```bash
python scripts/run_phase4.py
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

## Phase 5 artifacts

```bash
python scripts/run_phase5_audio.py
python scripts/run_phase5_alignment.py --language es
python scripts/run_phase5_beat_timing.py
python scripts/run_phase5_shot_timing.py
```

Produces:

```text
data/output/phase5/
├── narration.wav
├── narration.json
├── narration_words.json
├── beat_timings.json
└── shot_timings.json
```

## Real Phase 5 validation

The same samurai production example validated the complete temporal chain:

- canonical WAV narration measured at **45.0 seconds**;
- **105** recognized timing words, beginning at `0.0` and ending at `44.58` seconds;
- real provider output included several point-like word timestamps, validating the relaxed
  `start == end` rule;
- **11** beat intervals covered the entire `0.0–45.0` second timeline without gaps or overlaps;
- the shot regeneration used for this run contained **8** shots;
- **8** deterministic shot intervals covered the same `0.0–45.0` timeline exactly;
- the historical Phase 3 run had produced 7 shots, demonstrating that downstream timing does not
  depend on hardcoded shot counts.

These results are sufficient to close Phase 5.

## Phase 1 compatibility

The Phase 1 `DirectorAgent` remains as an experiment and regression fixture. Its rich scene type
has been renamed to `StoryboardScene` so it cannot be confused with the production `Scene`
contract.

The Phase 1 flow is not the long-term production architecture.

## Planned media stack

- **LLM orchestration:** OpenAI Responses API + Structured Outputs.
- **Storyboard planning:** provider-neutral shot keyframes before GPU video generation.
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
9. `SpeechProvider` + narration workflow: `SourceScript -> NarrationAudio + WAV`
10. `TranscriptionProvider`: `NarrationAudio -> NarrationWord[]`
11. `BeatTimingBot` + reconstruction: `Beat[] + NarrationWord[] -> BeatTiming[]`
12. Deterministic timing projection: `Shot[] + BeatTiming[] -> ShotTiming[]`

Real Phase 3 validation covered both stateful patterns. Real Phase 4 validation covered contextual
prompt design and provider-backed reference-image generation. Real Phase 5 validation covered the
complete 45-second audio-to-shot timeline.

## Next implementation step

**Phase 6 — Storyboard and per-shot visual planning.**

The next stage should generate a provider-neutral keyframe plan for each shot using the existing
`Shot`, canonical continuity entities/references and measured `ShotTiming` as context. The first
increment should persist prompts only. Image generation and scene grids should follow only after the
prompts have been inspected against the real samurai example.
