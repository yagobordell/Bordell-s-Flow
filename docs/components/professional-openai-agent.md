# OpenAI Professional director — CLI, validation and feedback

This is the NEW Professional-only path. Casero uses its existing bots unchanged.
Professional now executes one OpenAI Agents SDK agent over the **whole script**,
with five sequential phases under Python validation: narrative groups (B1.1
boundaries only), beats + image/video/editor planning, global evaluation using
Python tools, correction, and validated delivery. Group boundaries are NOT
independent planning requests.

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

## Phase logs and saved outputs

- `agent_openai/usage.json` records tokens, cache hits, request count and wall time
  of every OpenAI phase/retry, and the CLI prints each completed phase immediately.
- `agent_openai/groups.json`: B1.1 anchors materialized from exact original script.
- `agent_openai/plan_initial.json`: first whole-video beats+visuals+Haiku candidate.
- `agent_openai/review.json`: model review and deterministic validator report.
- `agent_openai/correction_N.json`: validated corrections and resegmentation attempts.
- `agent_openai/approved_plan.json`: immutable-script hash, final plan and validation.
- `agent_openai/editor_plan.json`: Haiku notes separate from the legacy Professional B2
  schema; `visual_plan.json` and `B2/merged_output.json` are also published.
- `agent_openai/conversation.db`: local SDK session with one conversation per run.

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
