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

Before making any paid request, the CLI announces whether a real ebook cover
is available. With no cover, `cta_group_id` and ebook references are prohibited
for all beats, including when the model guesses a promotional CTA. Python reports
`CTA_COVER_UNAVAILABLE` as a correctable validation issue rather than
throwing away the full paid phase-two result. This does not replace genuine
ebook promotion: to produce an authentic ebook CTA, configure `--ebook-cover`
or the avatar's saved ebook cover metadata before running.

The deterministic validation includes a frozen B1.1 narrative_core comparison.
No candidate plan is considered green until source tiling, narration length,
carrier/audio limits, source-attached editor anchors, CTA/ebook gating and the
legacy B2 and image-grid preflights pass.

## Phase logs and saved outputs

- `agent_openai/usage.json`: tokens, cached tokens, requests and wall time per
  SDK call, including structured JSON failures.
- `B1.1/output.json`: B1.1-compatible output, written as soon as group anchors
  are validated. Also stored in `agent_openai/phases/01_groups/output.json`.
- `B1.2/block_N/output.json` and `B1.2/merged_output.json`: ORIGINAL-SOURCE
  B1.2 beat output per group and merged, written as soon as every beat boundary
  is validated, before any paid model request for visuals. The global cut
  decisions and diagnostics are saved in `agent_openai/phases/02_beats/`.
- `B2/candidate_output.json`: candidate audiovisual plan even if it fails
  later validations. `B2/block_N/output.json` and `B2/merged_output.json`
  are written only after the strict B2 and grid preflight passes.
- `agent_openai/phases/02_visuals/output.json`, `03_evaluation/output.json`,
  `04_correction/output.json`, `05_delivery/output.json`: complete staged
  outputs and validation reports. Failed correction attempts are saved by number.
  A phase-04 no-op is recorded even when no new model call was needed.
- `B2.1/review_output.json`: archived global editorial B2.1-style review in
  its NEW contract; it does not masquerade as a legacy B21Output response.
- `agent_openai/groups.json`, `beats.json`, `plan_initial.json`,
  `review.json`, `correction_N.json`, `approved_plan.json`: audit checkpoints
  on the real source and all accepted creative revisions.
- `agent_openai/editor_plan.json`: separate source-anchored Haiku notes.
  `visual_plan.json`: full legacy-compatible visual plan for downstream.
  The runner later adds avatar metadata and CTA placement references.

**Checkpoints survive even when a later phase fails**: a bad visual generation
cannot delete an already validated B1.1/B1.2 result. Each logical phase
has an `output.json`, and B1.1/B1.2/B2 retain their familiar legacy
paths. There is no persistent SDK conversation database: avoiding redundant
whole-video history prevents unnecessary repeated-context token consumption.

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
