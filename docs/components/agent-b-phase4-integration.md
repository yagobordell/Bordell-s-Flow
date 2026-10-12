# Agent B — Phase 4 integrated opt-in workflow

All production model instructions remain English. The new integrated runner is opt-in; the legacy command remains the default.

## Setup

Select branch feat/agent-b-foundation-2026-10-12 in VS Code, pull and run the existing PowerShell setup_agent_b_preview.ps1. It installs the SDK in .venv-agent-b and does not modify uv.lock. Use the ignored root .env for OPENAI_API_KEY.

## Integrated dry media run (stops after B1.1 + Agent B + CTA detector)

Use a new, pending managed script and avatar in your local data library:

    .\run.ps1 YOUR_SCRIPT YOUR_AVATAR --agent-b --style profesional --stop B2 --no-audio --no-image

Only the model calls for B1.1, the single Agent B director and auxiliary professional CTA detection can incur charges. No voice, image or LTX generation occurs. B1.2/B2/B2.1 are not independent creative model calls in this mode. `--stop B2` ends immediately after the unified plan and CTA ownership verification. Use a new script/run folder for a new experiment.

The same command with --stop B2.1 is supported if an existing workflow expects that checkpoint label, but B2.1 is not an independent bot invocation.

Change only the Agent B model by adding --agent-b-model gpt-5.1. B1.1 keeps the existing configured model.

Run without --agent-b to retain the old locked environment and creative bot path. run.ps1 invokes .venv-agent-b/Scripts/python.exe only for explicit --agent-b; rerun setup if this environment is absent.

## Artifacts and failure diagnostics

The only public planning result is visual_plan.json; its `planning_origin=agent_b` marker selects the new global image-grid engine. The authoritative frozen project plan and session are stored under agent_b/ in the private run output. If the planner fails, inspect agent_b/agent_b_attempts.json; it lists controller failures, calls and saved draft IDs. CTA ownership disagreement fails closed before paid media generation.

Existing audio, image, preview and video consumers receive deterministic internal B1.2/B2 typed views derived from the ONE accepted plan, not independent AI bot outputs. The original B1.1 source text is immutable and the protected CTA detector checks the plan. Legacy b-pipeline-v1 files **without** planning_origin=agent_b use the existing numeric image grid logic; the version string alone never activates the new grid engine.

## Paid media canary after plan review

Only when a planning-only run is correct, use a fresh managed script with:

    .\run.ps1 ANOTHER_SCRIPT YOUR_AVATAR --agent-b --style profesional --no-video

This will permit real text, Gemini speech, OpenAI image and preview costs but skip LTX. This is not part of the first recommended smoke test. Image-conditioned motion reentry, automatic pre-freeze CTA reconciliation, and calibrated learning are not yet implemented.

The existing API ledger does not yet include token charges from the separate Agents SDK; the run report explicitly labels this accounting gap. Consult your OpenAI account usage totals for true usage. No automatic merge to main is performed.

## CI

CI tests the opt-in B1.1 -> Agent B -> CTA detector sequence with stubs, the typed adapter, the global grid planner and a simulated PNG provider. The Windows workflow checks run.ps1 parsing and setup PowerShell. It does not call a billable LLM or create actual media.
