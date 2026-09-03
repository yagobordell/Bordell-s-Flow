# Production Architecture

## Core decision

The production pipeline starts from a **finished script**. Script generation is optional and lives
outside the core production flow.

```text
Optional topic -> ScriptWriterAgent -> generated Script
                                  \
                                   -> SourceScript -> production pipeline
User-written script ---------------/
```

`SourceScript` is deliberately minimal and contains only the script text.

## Bots before agents

Production planning uses bounded bots: one stable instruction set, one well-defined task and one
Structured Output contract. Bots do not decide which tool to call or autonomously restructure the
workflow.

Autonomous agents are reserved for later verification stages, where a verifier may inspect an
artifact and request selective regeneration.

## Ownership principle

The project repeatedly applies the same rule:

> The model owns semantic decisions. Python owns canonical identity, ordering, reconstruction,
> validation and persistence.

Examples:

- narrative segmentation: model chooses boundaries, Python reconstructs original text;
- beat timing: model chooses ending word IDs, Python reconstructs timestamps;
- storyboard prompts: model chooses a visual composition, Python keeps the canonical `shot_id`;
- grids: no model is needed because grouping already exists in `Shot.scene_id`.

This keeps model output small and makes downstream stages auditable.

## Current production hierarchy

### Narrative hierarchy

```text
SourceScript
  -> NarrativeBlock[]
      -> Beat[]
          -> Scene[]
              -> Shot[]
```

### Continuity hierarchy

```text
ContinuityEntity[]
  -> VisualReference[]
      -> ReferenceAsset[]
```

### Temporal hierarchy

```text
SourceScript.text
  -> NarrationAudio
      -> NarrationWord[]
          -> BeatTiming[]
              -> ShotTiming[]
```

### Storyboard hierarchy

```text
Shot[] + ShotTiming[] + VisualReference[]
                  -> StoryboardFrame[]

StoryboardFrame[] + Shot[] + ReferenceAsset[]
                  -> StoryboardKeyframe[]

Scene[] + Shot[] + StoryboardKeyframe[]
                  -> StoryboardGrid[]
```

## Canonical contracts

Definitions:

- **NarrativeBlock**: contiguous semantic section of the immutable source script.
- **Beat**: one visualizable action, change or idea belonging to one narrative block.
- **Scene**: ordered grouping of beat IDs.
- **Shot**: minimal audiovisual unit planned from consecutive beats inside one scene.
- **ContinuityEntity**: recurring visual identity tracked across narrative context.
- **VisualReference**: provider-neutral canonical visual description for one entity.
- **ReferenceAsset**: persisted reference-image URI bound to one entity.
- **NarrationAudio**: canonical narration URI plus measured playback duration.
- **NarrationWord**: recognized word and timestamps used only as timing evidence.
- **BeatTiming**: canonical narration interval assigned to one beat.
- **ShotTiming**: deterministic projection of beat timing onto one shot.
- **StoryboardFrame**: provider-neutral still-image prompt for one canonical shot.
- **StoryboardKeyframe**: persisted still-image URI bound to one canonical shot.
- **StoryboardGrid**: persisted scene-level review grid URI.
- **GPUDeviceProfile**: immutable GPU identity and total VRAM observed before a benchmark.
- **LTXBenchmarkProfile**: one reproducible model, pipeline, shape and runtime configuration.
- **LTXBenchmarkSample**: one measured run with duration, peak VRAM and output identity.
- **LTXBenchmarkReport**: hardware, sanitized command, samples and aggregate benchmark evidence.

The contracts remain intentionally small:

```text
NarrativeBlock     = { id, text }
Beat               = { id, block_id, action }
Scene              = { id, beat_ids }
ContinuityEntity   = { id, kind, name, description }
BlockContinuity    = { block_id, entity_ids }
Shot               = { id, scene_id, beat_ids, entity_ids, action }
VisualReference    = { entity_id, prompt }
ReferenceAsset     = { entity_id, uri }
NarrationAudio     = { uri, duration_seconds }
NarrationWord      = { id, text, start_seconds, end_seconds }
BeatTiming         = { beat_id, start_word_id, end_word_id, start_seconds, end_seconds }
ShotTiming         = { shot_id, start_seconds, end_seconds }
StoryboardFrame    = { shot_id, prompt }
StoryboardKeyframe = { shot_id, uri }
StoryboardGrid     = { scene_id, uri }
GPUDeviceProfile    = { index, name, memory_total_mib }
LTXBenchmarkProfile = { label, ltx_source, pipeline, quantization, offload, dimensions, runs }
LTXBenchmarkSample  = { run_index, duration_seconds, peak_gpu_memory_mib, output metadata }
LTXBenchmarkReport  = { profile, command, devices, samples, aggregate metrics }
```

Provider parameters such as model, quality, resolution, speech voice or image-edit options are not
stored in these contracts unless a downstream stage genuinely needs them.

## Continuity registry

Phase 3 keeps continuity separate from narrative structure rather than mutating
`NarrativeBlock`, `Beat` or `Scene`.

Supported entity kinds:

```text
character
group
location
object
```

`object` means a physical tangible object. Abstract concepts, values, doctrines or mental states
remain narrative concepts and do not receive continuity IDs.

The model never assigns canonical IDs. It decides whether known entities reappear and describes
new entities. Python assigns deterministic type-prefixed IDs such as `character_001` and
`location_001`.

Entities may remain contextually active when the narrative clearly continues in the same place or
situation. They are not retained merely because they appeared earlier.

## Orchestration patterns

### Parallel fan-out / fan-in: beat extraction

Independent narrative blocks are processed concurrently:

```text
block 1 -> BeatExtractorBot --\
block 2 -> BeatExtractorBot ----> Beat[] -> ScenePlannerBot
block N -> BeatExtractorBot --/
```

`asyncio.gather` performs fan-out. Python restores deterministic global beat IDs after calls finish.

### Stateful serial continuity

Continuity-sensitive work runs in order:

```text
block 1 -> ContinuityBot -> response_id_1
                              |
                              v
block 2 -> ContinuityBot -> response_id_2
                              |
                              v
block N -> ContinuityBot -> ...
```

Each turn receives the current block, Python's canonical entity registry and the previous provider
response ID.

### Stateful serial shot planning

Shot planning uses a separate state chain:

```text
scene 1 -> ShotPlannerBot -> shot_response_id_1
                              |
                              v
scene 2 -> ShotPlannerBot -> shot_response_id_2
                              |
                              v
scene N -> ShotPlannerBot -> Shot[]
```

Provider state is local to one bot process. Canonical continuity crosses stages through persisted
IDs and artifacts, not hidden shared model memory.

The model groups consecutive beats and chooses active entity IDs. Python validates exact beat
coverage, ordering, scene relationships and entity validity before assigning global shot IDs.

### Parallel fan-out / fan-in: visual references

Once continuity identities are resolved, entity design is independent:

```text
entity 1 + narrative context -> VisualReferenceBot --\
entity 2 + narrative context -> VisualReferenceBot ----> VisualReference[]
entity N + narrative context -> VisualReferenceBot --/
```

Narrative context is derived by Python from `NarrativeBlock[]` and `BlockContinuity[]`. It is
temporary evidence and is not copied into the persisted contract.

### Parallel fan-out / fan-in: reference assets

Validated visual-reference prompts are rendered independently:

```text
VisualReference 1 -> ImageProvider --\
VisualReference 2 -> ImageProvider ----> GeneratedImage[] -> ReferenceAsset[]
VisualReference N -> ImageProvider --/
```

Binary payloads remain ephemeral until all generations succeed. Only then are deterministic files
written. This prevents a provider failure from leaving a partially committed batch.

### Single canonical narration

Phase 5 deliberately creates one continuous narration instead of TTS per shot:

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

This preserves prosody and prevents visual planning from forcing artificial audio cuts.

Duration is measured from the PCM frames actually present rather than trusting a potentially
streaming/sentinel WAV data-size header.

### Word-level timing evidence

```text
SourceScript.text + narration.wav
              ↓
TranscriptionProvider
              ↓
NarrationWord[]
```

`NarrationWord.text` is recognized evidence, not canonical narrative text. Small ASR artifacts are
preserved rather than silently rewriting `SourceScript.text`.

Point-like timestamps where `start_seconds == end_seconds` are valid when the global sequence is
non-decreasing and remains inside the measured narration duration.

### Model-owned beat boundaries, Python-owned time

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

The model never generates timestamps. For non-final beats, Python ends an interval at the start of
the next beat's first word. The final beat ends at `NarrationAudio.duration_seconds`.

This assigns inter-beat pauses to the previous visual state and yields a continuous timeline.

### Deterministic shot timing

```text
Shot.beat_ids + BeatTiming[]
            ↓
Python
            ↓
ShotTiming[]
```

A shot starts at its first beat and ends at its last beat. Python validates exact beat coverage,
ordering and continuity. No LLM is required.

The pipeline therefore does not depend on a fixed number of shots.

## Phase 4 visual-reference boundary

`VisualReferenceBot` returns a stable description for one entity. Python applies a fixed template
selected by entity kind.

Current templates conceptually cover:

```text
character -> neutral full-body identity reference
group     -> representative shared-appearance reference
location  -> single coherent environment reference
object    -> isolated tangible-object reference
```

Templates avoid temporary shot action, visible text, labels and watermarks. `visual_style` remains
a runtime input rather than part of continuity identity.

Broad locations are contextualized into one reusable physical environment instead of becoming an
encyclopedic montage. This rule was introduced after a real validation where a broad `Japan`
reference initially drifted toward a modern-country interpretation.

## Phase 6 storyboard architecture

### 6.1 Provider-neutral storyboard prompts

Inputs:

```text
Shot[]
ShotTiming[]
VisualReference[]
```

Output:

```text
StoryboardFrame = { shot_id, prompt }
```

`StoryboardFrameBot` runs serially **within each scene**. The current shot receives:

- `Shot.action`;
- the measured shot duration;
- canonical visual-reference descriptions for `Shot.entity_ids`;
- the previous storyboard prompt when still inside the same scene.

At a scene boundary, previous-frame context is reset. Identity continuity still comes from canonical
references, while scene composition is free to change.

The bot is explicitly instructed that continuity does not mean repetition. If narrative meaning
changes, subject, state, composition or context should change visibly.

For abstract ideas such as legacy, memory or symbolic influence, the bot translates the concept
into physical visual evidence grounded in `Shot.action` rather than merely making the same image
more dramatic.

The prompt is still a **single static keyframe**. Camera movement, transitions and video-generation
instructions remain deferred to video-generation/composition stages.

### 6.2 Reference-conditioned keyframe generation

Inputs:

```text
StoryboardFrame[]
Shot[]
ReferenceAsset[]
```

Output:

```text
StoryboardKeyframe = { shot_id, uri }
```

For each shot, Python resolves only the reference assets named by `Shot.entity_ids`. Unrelated
reference images are not sent to the provider.

`ReferenceAwareImageProvider` adds a second provider-neutral capability without changing the
original Phase 4 `ImageProvider.generate_image()` contract:

```text
no references  -> generate_image(...)
with references -> generate_image_with_references(...)
```

The current OpenAI implementation uses image editing with multiple image inputs for the
reference-conditioned path.

Model-specific parameters are deliberately optional. A real Phase 6 validation showed that
`gpt-image-2` accepts the image references but rejects an explicit `input_fidelity` parameter. The
provider therefore omits optional fidelity controls unless the caller explicitly requests them.
This prevents capability assumptions from leaking into the provider-neutral contract.

Keyframes are generated **independently and concurrently**. The previous generated PNG is not fed
into the next shot. This avoids propagating one visual defect through the whole sequence and keeps
future selective regeneration possible.

As with Phase 4 assets, all provider calls complete before the workflow writes the batch.

### 6.3 Deterministic scene grids

Inputs:

```text
Scene[]
Shot[]
StoryboardKeyframe[]
```

Output:

```text
StoryboardGrid = { scene_id, uri }
```

This stage uses no LLM and no external API. Pillow composes scene-level contact sheets from the
canonical keyframes.

Python validates:

- unique scene and shot IDs;
- exact keyframe/shot ID alignment;
- known `scene_id` references;
- scene order and contiguity of shots;
- safe relative asset URIs;
- existing, decodable PNG keyframes;
- positive layout dimensions.

The layout preserves shot order, keeps keyframe aspect ratio without cropping, uses up to three
columns and adds rows automatically. Scene and shot labels exist only inside the review grid; they
do not modify the canonical keyframe files.

All grids are composed in memory before persistence so invalid input does not leave a partial
scene-grid batch.

## Phase 7 GPU infrastructure

### 7.1 Benchmark before worker shape

Phase 7 begins with measurement rather than a hardcoded Salad GPU profile. The benchmark accepts an
auditable LTX-2.5 command template and runs it directly without a shell:

```text
LTXBenchmarkProfile + command template
                   ↓
             warmup runs
                   ↓
 measured runs + nvidia-smi sampling
                   ↓
 validate MP4 + SHA-256
                   ↓
          LTXBenchmarkReport
```

Python owns the run count, output placeholder, timing, validation, hashing, aggregation and
persistence. LTX owns inference. `nvidia-smi` is an evidence provider only; its peak value covers
total memory used on each visible GPU, so benchmark nodes must not run unrelated workloads.

The report stores the complete command needed for audit, but common inline token, API-key, password
and object-storage credential forms are redacted first. Output files are removed before every run,
so a successful process cannot accidentally validate a stale artifact.

Warmups are deliberately excluded from aggregate timings. Measured samples retain their individual
duration, peak memory, size and SHA-256 so later hardware selection does not depend only on one
average.

The validated Phase 7 baseline is an RTX 5090 using `fp8-cast` with CPU offload. A real matrix with
one warmup and three measured runs produced a 194.93-second mean, 0.621 end-to-end FPS and a
24,513 MiB peak for 121 frames at 768x1280. This is a provisional production starting point rather
than a permanent hardware lock; future matrices may replace it without changing the worker
boundary. LTX and its model-sized checkpoint set remain outside the application package and are
loaded in the target GPU environment.

`run_phase7_benchmark_matrix.py` expands the canonical bf16/fp8/offload cases on one hardware
target and writes one report per case plus a local matrix. `summarize_phase7_benchmarks.py` then
compares matrices from different GPUs, rejects workload drift and records the SHA-256 of every
source matrix. It identifies the fastest measured case but intentionally does not replace the
required visual-quality, availability and cost decision.

### 7.2 implemented worker boundary

The worker is a versioned HTTP boundary compatible with Salad Job Queue:

```text
Salad input -> GPUJobRequest -> transactional claim + lease
                                  /                 \
                         R2 inputs            task runner
                                                   |
                                            R2 deterministic output
                                                   |
                                         Postgres success commit
```

`GPUJobRequest` owns the application `job_id`; Salad's `Salad-Job-Id` remains a transport ID.
Canonical JSON produces an immutable request SHA-256. Reusing one application ID with a different
request is a conflict.

The output key must be scoped below `jobs/<job_id>/`. R2 metadata stores the job, request and
artifact hashes. A completed Postgres row is replayed. If R2 contains the matching artifact but
Postgres does not yet contain the success commit, the worker reconciles it without executing the
task again. Foreign output metadata is never overwritten.

Postgres owns atomic claims, attempt counts and expiring leases. A background heartbeat renews the
lease and the worker performs a synchronous renewal immediately before upload. Losing ownership
prevents the artifact commit.

Phase 7 registers only `infrastructure.copy`, a deterministic smoke task. Phase 8 will register the
direct Python/PyTorch LTX runner without changing storage, lease or HTTP semantics.

### 7.3 operational validation and closure

The cloud smoke completed on 27 August 2026 with one attempt and identical input/output SHA-256.
`replay_phase7_smoke.py` then resubmitted the exact saved request and returned `replayed=true`, the
same artifact identity and the unchanged attempt count. The production worker image was observed
by registry digest, and the worker group was returned to zero replicas.

Deployment manifests include the HTTP probe's required empty `headers` list. The renderer requires
a registry reference pinned as `repository@sha256:<digest>` by default; mutable tags are available
only through an explicit debugging override. Both facts were observed in Salad. The later LTX-2.5
benchmark also completed on real RTX 5090 hardware, its JSON and MP4 passed validation, and its
group was stopped. Phase 7 is closed; the evidence inventory is in `docs/phase7-closure.md`.

## Media provider boundaries

### Structured text

`StructuredTextProvider` handles independent Structured Output transformations.
`StatefulStructuredTextProvider` adds provider-managed state:

```text
StatefulStructuredResult
  output
  response_id
```

`OpenAIProvider` implements both. Python remains authoritative for IDs, ordering and relationships.

### Images

Base generation:

```text
ImageProvider.generate_image(...)
  -> GeneratedImage
       content
       media_type
       extension
```

Reference-aware generation:

```text
ReferenceAwareImageProvider.generate_image_with_references(...)
  -> GeneratedImage
```

`GeneratedImage.content` is ephemeral. Persisted domain objects store URIs, not provider response
objects or base64 payloads.

`OpenAIImageProvider` is the initial implementation. Model, quality, size and optional edit controls
remain runtime concerns.

### Speech

```text
SpeechProvider.generate_speech(...)
  -> GeneratedSpeech
       content
       media_type
       extension
```

The initial implementation is `OpenAISpeechProvider`. Voice, model, speed and instructions are
runtime generation parameters.

### Transcription

`TranscriptionProvider` exposes normalized word-level timing evidence independently of TTS.
The domain persists `NarrationWord[]`, not raw provider responses.

## Persisted artifacts by phase

### Phase 2

```text
data/output/phase2/
├── source_script.json
├── narrative_blocks.json
├── beats.json
└── scenes.json
```

### Phase 3

```text
data/output/phase3/
├── entities.json
├── block_continuity.json
└── shots.json
```

### Phase 4

```text
data/output/phase4/
├── visual_references.json
├── reference_assets.json
└── reference_assets/
    ├── group_001.png
    ├── group_002.png
    └── location_001.png
```

### Phase 5

```text
data/output/phase5/
├── narration.wav
├── narration.json
├── narration_words.json
├── beat_timings.json
└── shot_timings.json
```

### Phase 6

```text
data/output/phase6/
├── storyboard_frames.json
├── storyboard_keyframes.json
├── storyboard_keyframes/
│   ├── shot_001.png
│   ├── ...
│   └── shot_008.png
├── storyboard_grids.json
└── storyboard_grids/
    ├── scene_001.png
    ├── scene_002.png
    └── scene_003.png
```

### Phase 7

```text
data/output/phase7/
└── ltx_benchmark.json
```

The filename may be changed per hardware/profile case. Generated benchmark MP4 files are temporary
and remain under `data/tmp/phase7/` by default.

## Real validation summary

### Phase 3

Stateful continuity and stateful shot planning were validated as separate provider-state chains.
The historical closing run produced 7 shots.

### Phase 4

The samurai example produced 3 reusable visual references and 3 canonical PNG assets:

- samurai group;
- differentiated feudal-lord group;
- coherent feudal Japanese environment.

### Phase 5

The same production example validated the complete temporal chain:

- canonical narration: **45.0 seconds**;
- recognized timing words: **105**;
- beat intervals: **11**, covering `0.0–45.0` exactly;
- regenerated shots for this run: **8**;
- shot intervals: **8**, covering `0.0–45.0` exactly.

The different historical shot counts demonstrate that timing projection does not depend on a
hardcoded count.

### Phase 6

The 8-shot regeneration was used for real storyboard validation:

- **8** `StoryboardFrame` prompts were reviewed and iterated semantically;
- the first shot was corrected so the visual represented warrior rule rather than an empty
  establishing shot;
- abstract legacy in the final shot was translated into armor, katana and pictorial/historical
  evidence rather than another repeated living-warrior pose;
- **8** reference-conditioned vertical keyframes were generated at `1024x1536`;
- identity and visual language remained coherent while compositions varied across shots;
- **3** deterministic scene grids were composed locally;
- scene 1 contains shots 1–4, scene 2 contains 5–6 and scene 3 contains 7–8;
- the grids preserved aspect ratio, order and readable shot labels without modifying keyframes.

These results are sufficient to close Phase 6.

## Phase 1 compatibility

The Phase 1 `DirectorAgent` remains an experiment and regression fixture. Its rich scene type is
`StoryboardScene` so it cannot be confused with the production `Scene` contract.

The Phase 1 flow is not the long-term production architecture.

## Planned media stack

- **LLM orchestration:** OpenAI Responses API + Structured Outputs.
- **Storyboard planning:** completed provider-neutral keyframe pipeline from Phase 6.
- **GPU inference:** Docker containers on Salad.
- **Video model:** LTX-2.5 with the validated RTX 5090 / `fp8-cast` / CPU-offload baseline.
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
7. Reference asset generation: `VisualReference[] -> ReferenceAsset[] + PNG files`
8. Canonical narration: `SourceScript -> NarrationAudio + WAV`
9. Word alignment: `NarrationAudio -> NarrationWord[]`
10. Beat timing: `Beat[] + NarrationWord[] -> BeatTiming[]`
11. Shot timing: `Shot[] + BeatTiming[] -> ShotTiming[]`
12. Storyboard prompting: `Shot[] + ShotTiming[] + VisualReference[] -> StoryboardFrame[]`
13. Keyframe generation: `StoryboardFrame[] + ReferenceAsset[] -> StoryboardKeyframe[]`
14. Scene grids: `Scene[] + Shot[] + StoryboardKeyframe[] -> StoryboardGrid[]`
15. Reproducible LTX matrix and multi-hardware comparison -> reports + auditable JSON
16. Versioned HTTP worker: `GPUJobRequest -> GPUJobResponse`
17. Cloudflare R2 adapter: streamed local files + object metadata reconciliation
18. Supabase/Postgres adapter: atomic claims, leases, retries and completed results
19. Salad Docker image: official queue worker v0.7.0 verified by SHA-256
20. Digest-pinned deployment renderer, smoke verifier and replay/idempotency verifier

## Next validation step

**Phase 8 — direct LTX-2.5 video generation.**

Phase 7 is closed: infrastructure smoke, idempotent replay, digest-pinned worker deployment, real
RTX 5090 benchmark, artifact validation and scale-down all passed. Phase 8 now adds the direct
LTX-2.5 Python/PyTorch task runner to the existing registry and returns R2 metadata through the same
idempotent contract rather than transporting video bytes through the orchestrator. A future
multi-hardware benchmark can refine the provisional baseline without reopening the infrastructure
contract.
