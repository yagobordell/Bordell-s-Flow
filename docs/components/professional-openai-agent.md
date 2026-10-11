# OpenAI Professional director — CLI, validation and feedback

This is the NEW Professional-only path. Casero uses its existing bots unchanged.
Professional now executes one OpenAI Agents SDK agent over the **whole script**,
with five logical phases under Python validation: narrative groups (B1.1),
global editorial segmentation followed by global image/video/Haiku planning
(B1.2/B2), global evaluation (B2.1-style review), localized corrections,
and deterministic green-only delivery. Group boundaries are NOT
independent model requests.

## Running

From the repository root on Windows (the supported human-facing managed launcher):

```powershell
.\run.ps1 mi_guion MiAvatar --style profesional --stop openai-agent
```

This runs the five OpenAI phases, prints their elapsed time and input/output/
cached tokens, saves all agent checkpoints, and exits **before Gemini audio,
OpenAI image generation and paid LTX jobs**. It is not a dry run of the OpenAI
calls: planning itself uses actual paid model tokens. The avatar/QR and optional
ebook cover are resolved by the existing local run preparation.

Full production without the stop switch:

```powershell
.\run.ps1 otro_guion MiAvatar --style profesional
```

Style is stored in managed avatar metadata. The preproduction agent is only
used in Profesional; Casero does not instantiate it. `--from B2` legacy
Professional resumes are intentionally blocked; the new director works on the
entire script and owns beat boundaries. Completed plans can still feed
`--images-only` and saved media workflows where their normal preflights pass.

The PowerShell entrypoint uses `uv run --locked --with "openai-agents>=0.22,<0.24"`
as an SDK overlay without modifying the existing `uv.lock`. For direct CLI
invocation use:

```bash
uv run --locked --with "openai-agents>=0.22,<0.24" --extra dev python scripts/pipeline/run_b_pipeline.py myscript.txt --style profesional --stop openai-agent --avatar AvatarName
```

## Phase-one cost and ebook capability gate

B1.1 emits one strict structured GroupProposal without model-side tool calls.
Each attempt is exactly one SDK model turn; Python materializes and validates
the exact source spans afterwards. A retry is charged only for an invalid
structured response or invalid source anchors. **No artificial max_tokens cap**
is configured on ANY phase.

Phase 2 is deliberately split into two global operations — NOT calls per block:
(2a) the one logical director applies the complete original professional
B1.2 beat-definition, semantic atomicity and YouTube viewing-cadence rules to
the entire script, returning only exact end-phrase anchors and beat types.
Python materializes all narration literally from the original script and
requires every beat <=100 stripped characters and <=40 words. If 31 beats are
too long, all 31 are rejected *before* a single expensive visual prompt call.
(2b) after all beats are valid, the director chooses visual carriers and
image/video/editor prompts over the whole video without reprinting beat text.

The whole-video review receives the current plan and ONE compact Python report,
not an SDK conversation containing every previous huge JSON/tool result.
Correction results are targeted visual patches rather than an expensive
re-generation of the entire script, unless the agent explicitly chooses
new narrative boundaries (which require a complete visual replacement only
for that affected narrative group). The director, not Python, decides cadence
and cuts; no target beats-per-minute, minimum shot duration or hidden automatic
rhythm scheduler is introduced. No SDK Session is used to accumulate repeated
script/plan/tool payloads across phases.

Before making any paid request, Python checks the source for an explicit
spoken ebook promotion tied to a QR scan. If an authentic cover is missing
for that protected CTA, the run fails **before any OpenAI request**, with
`PREFLIGHT_EBOOK_COVER_REQUIRED`. Configure `--ebook-cover` or the
avatar's saved cover asset and rerun. Non-ebook videos remain usable
without a cover. In all cases an unreferenced, invented ebook CTA is blocked
by deterministic validation; editorial correction cannot silently replace
the real user's promotion with a fictitious asset.

The deterministic validation includes a frozen B1.1 narrative_core comparison.
No candidate plan is considered green until source tiling, narration length,
carrier/audio limits, source-attached editor anchors, CTA/ebook gating and the
legacy B2 and image-grid preflights pass.

## Phase logs and saved outputs

- `agent_openai/usage.json`: tokens, cached tokens, requests and wall time per
  SDK call, including structured JSON failures.
- `agent_openai/phases/01_groups/output.json`: complete phase 1 grouping,
  exact anchor validation and B1.1-compatible metadata.
- `agent_openai/phases/02_planning/output.json`: complete phase 2 planning
  and validation. Its subfolders `beats/` and `visuals/` hold the two
  global model passes, exact source cuts, first visual plan and diagnostics.
  These are **one logical editorial phase**, not separate independent bots.
- `agent_openai/phases/03_evaluation/output.json`: one complete-video
  editorial assessment and Python validation report.
- `agent_openai/phases/04_correction/output.json`: all targeted correction
  attempts and deterministic before/after results; no-op if nothing needs
  correction.
- `agent_openai/phases/05_delivery/output.json`: authoritative validated
  production-ready plan and final gate, or concrete blocking issues.
- `_pipeline_compat/B1.1/`, `_pipeline_compat/B1.2/`,
  `_pipeline_compat/B2/` and `_pipeline_compat/B2.1/` contain ONLY
  internally required legacy bridge artifacts and remain isolated from
  the five editorial phase outputs. The agent still writes its internal
  work-stage files where the downstream pipeline expects them.
- `agent_openai/groups.json`, `beats.json`, `plan_initial.json`,
  `review.json`, `correction_N.json`, `approved_plan.json`: audit checkpoints
  on the real source and all accepted creative revisions.
- `agent_openai/editor_plan.json`: separate source-anchored Haiku notes.
  `visual_plan.json`: full legacy-compatible visual plan for downstream.
  The runner later adds avatar metadata and CTA placement references.

**Checkpoints survive even when a later phase fails**: a bad visual generation
cannot delete an already validated B1.1/B1.2 result. Each logical phase
has an `output.json`, and B1.1/B1.2/B2 retain their familiar legacy
paths. The managed run synchronizer copies the five phase outputs
into `data/<avatar>/<script>/outputs/agent_openai/phases/`, including
failed and corrected attempts. Legacy B-stage payloads are visible only
under `outputs/_pipeline_compat/`. Completed publications preserve the
same structure. Casero's established output structure remains unchanged. There is no persistent SDK
conversation database: avoiding redundant whole-video history prevents
unnecessary repeated-context token consumption.

A phase failure leaves checkpoints and stops production, rather than letting an
unchecked visual plan reach paid imagery or GPU jobs. The rhythm map is based on
relative script positions until narration/STT produces actual word/frame timing.
There can be 1–4 images within one `media_image` beat; >=70 non-CTA characters
requires at least two. The compositor divides actual beat frames among subimages.

## Feedback — proposal and approval, never automatic rule rewrites

After a run, for literal comments:

```powershell
.\run.ps1 mi_guion MiAvatar --feedback-gpt "Repetiste el mismo plano varias veces"
```

Or put detailed feedback in `feedback.md` and pass the filename with `@`:

```powershell
.\run.ps1 mi_guion MiAvatar --feedback-gpt "@feedback.md"
```

The agent receives the full original script, the approved plan and your
comment. It saves a **pending** JSON proposal under
`agent_openai/feedback/proposal_*.json`: an analysis, suggested additions to
`groups.md` and `beats_visuals.md`, and any suggested instruction/rule
clarifications for human review. No instructions or memory change yet.

Inspect this JSON first, then explicitly approve:

```powershell
.\run.ps1 mi_guion MiAvatar --approve-gpt-feedback ".video_factory_work/MiAvatar/mi_guion/agent_openai/feedback/proposal_<id>.json"
```

Approval appends only the reviewed memory entries to
`data/_professional_gpt_memory/groups.md` and `beats_visuals.md`
(or the corresponding configured `--avatars-dir` directory). Future
Professional executions load these persistent memories after immutable rules.
Proposed changes to `phases/*.md` or `rules/*.md` **are not applied** by
feedback approval; a human must inspect and change version-controlled prompt
instructions explicitly. This protects critical original B2 screens/documents,
motion, CTA and voice contracts.

## Deployment precautions

The combined model+render path must still be tested on actual projects and
real OpenAI credentials. Offline CI validates the code, mock agent phases and
contracts, not live model quality or LTX output. A stopped agent-only run
does not create audio; resuming audio later requires valid voice and STT state.

## Global correction and visual-direction quality gate

Phase 04 performs at most one global creative/structural correction by default
after the phase-03 review. Its single structured answer may patch every
affected beat and resegment explicitly necessary B1.2 groups, while Python
verifies all agreed invariants. Further expensive creative retries are NOT
silently launched; unresolved issues remain available as failed phase outputs.
A malformed JSON SDK reply may still use bounded transport retries.

The full-model visual directions require source-owned concrete actor actions,
same-identity cross-beat chronological continuity, physically possible
single-shot events and semantic progression instead of repeated generic
phone imagery. Meaningful continuous still sequences can share a single
numbered grid across multiple beats, including one-image beats. Python
validates asset count and performs numbering; it does not decide pacing.

Source-critical readable screen or document information and branded symbolic
detail are planned as source-anchored postproduction overlays (using truly
available/verified assets), rather than synthesized into generated screen or
card pixels. The original absolute screen/document rule remains binding.
Important conceptual headlines and contrast/reveal moments are also
candidates for Haiku editor notes, determined editorially from the
complete story, not from any fixed beat type or quota.

The authoritative five exported phase folders are 01_groups,
02_planning, 03_evaluation, 04_correction, and 05_delivery. Subpasses
inside 02_planning do not create independent public editorial stages.

Prompt authoring follows official OpenAI recommendations for clear,
positive, specific instructions and explicit structured output contracts.
Per editorial direction, the Markdown instructions deliberately do
not contain input/output few-shot cases or scenario-by-scenario templates.
