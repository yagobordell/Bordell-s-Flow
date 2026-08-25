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
              -> Shot[]   (future phase)
```

Definitions:

- **NarrativeBlock**: contiguous semantic section of the script.
- **Beat**: one visualizable action, change or idea belonging to one block.
- **Scene**: ordered grouping of beat IDs.
- **Shot**: future audiovisual unit generated from a scene.

The contracts remain intentionally small:

```text
NarrativeBlock = { id, text }
Beat           = { id, block_id, action }
Scene          = { id, beat_ids }
```

A bot must emit only the variables required by the next stage. Camera, lighting, prompts,
transitions, characters and locations are not fields of these early narrative contracts.

## Planned orchestration

### Parallel stages

Independent narrative blocks can be processed concurrently:

```text
block 1 -> BeatExtractorBot --\
block 2 -> BeatExtractorBot ----> Beat[] -> ScenePlannerBot
block N -> BeatExtractorBot --/
```

This is a fan-out / fan-in stage and can later use `asyncio.gather` or a workflow engine.

### Stateful serial stages

Continuity-sensitive work runs in order and retains prior context:

```text
block 1 -> context -> block 2 -> context -> block 3
```

Planned examples:

- continuity and entity assignment, block by block;
- shot planning, scene by scene.

These stages may use the OpenAI Responses API conversation state while still requesting
Structured Outputs on every step.

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

## Next implementation step

Phase 2 of the revised roadmap implements three bounded bots:

1. `NarrativeBlockBot`: `SourceScript -> NarrativeBlock[]`
2. `BeatExtractorBot`: `NarrativeBlock -> Beat[]` (parallel per block)
3. `ScenePlannerBot`: `NarrativeBlock[] + Beat[] -> Scene[]`

Every bot will return the smallest possible Pydantic model through Structured Outputs.
