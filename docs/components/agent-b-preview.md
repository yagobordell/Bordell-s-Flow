# Agent B — VS Code isolated planning preview (Phase 2)

This preview uses **one autonomous Agent B** to produce a single validated
audiovisual plan from already-materialized, unmodified B1.1 blocks.
It is not connected to the legacy B1.2/B2/B2.1 jobs or to the media pipeline.
This guide and all runtime instructions are in English.

## 1. Check out the implementation branch

In a terminal at the repository:

```bash
git fetch origin
git switch feat/agent-b-foundation-2026-10-12
```

Do **not** use `main` for this preview; the implementation is in PR #336.

## 2. Install the preview environment (Windows / VS Code)

Requirements: Python 3.12, `uv` 0.12.18, VS Code, Python and Python
Debugger extensions. A normal `uv sync --locked` is not used in this
isolated preview.

From the VS Code PowerShell terminal at the repository root, run:

```powershell
.\scripts\pipeline\setup_agent_b_preview.ps1
```

The script creates **`.venv-agent-b`** and installs the project in editable
mode with dev tools plus pinned **`openai-agents==0.23.1`**. It verifies
dependency consistency, then performs a true Agents SDK **offline preflight**.

It does not alter `pyproject.toml`, `uv.lock`, the production `.venv`,
or the media pipeline. Repeating setup updates the isolated preview environment.

If PowerShell blocks execution, use an execution-policy override only for
that terminal session, not a permanent machine-wide override:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\pipeline\setup_agent_b_preview.ps1
```

Select the **`.venv-agent-b\Scripts\python.exe`** interpreter in VS Code
via **Ctrl+Shift+P → Python: Select Interpreter**. Keep that interpreter selected
for debugging Agent B. The profile does not override global Python settings.

## 3. Linux/macOS alternative

From the repository root:

```bash
uv venv --python 3.12 .venv-agent-b
uv pip install --python .venv-agent-b/bin/python -e ".[dev]" \
  -r config/agent-b-preview-requirements.txt
uv pip check --python .venv-agent-b/bin/python
.venv-agent-b/bin/python -m scripts.pipeline.run_agent_b_preview \
  --preflight \
  --input examples/agent_b_preview_phone.json \
  --project .video_factory_work/agent_b_preview_phone \
  --model gpt-5.1
```

Select `.venv-agent-b/bin/python` as VS Code's Python interpreter.

## 4. VS Code F5: offline preflight first

Open **Run and Debug** (Ctrl+Shift+D), choose:

**Agent B — preflight (offline, no API)**

Press **F5**. You should see a JSON result with:

```json
{
  "status": "preflight_ok",
  "api_calls": 0,
  "block_count": 3,
  "tool_count": 11,
  "project_status": "planning"
}
```

Additional fields (the tool names, SHA-256 instruction fingerprint, model,
revision and project path) are printed too. The exact revision may differ if
you have already run the preview. This check imports **the installed real SDK**,
constructs the `Agent` and `SQLiteSession`, registers all 11 callable
Python tools, verifies that B1.1 data is read verbatim and records a private
project checkpoint. It **never calls an OpenAI model**.

The example is at `examples/agent_b_preview_phone.json` and covers the
verbatim scam-call suspense example in three B1.1 source blocks. Edit or copy
this file to test a different immutable B1.1 output; change the project
directory as well to prevent cross-video context contamination.

## 5. VS Code F5: real Agent B planning

Make a local `.env` at the repository root (already ignored by Git).
Copy `.env.example` if needed and set:

```dotenv
OPENAI_API_KEY=YOUR_OWN_KEY_HERE
```

Never put credentials into `launch.json`, an example input file, a
GitHub commit or chat. The CLI loads the ignored `.env` itself and the
VS Code debug configuration also supports it through `envFile`.

Choose **Agent B — real planning (uses OpenAI API)**, then press **F5**.

The preview defaults to model `gpt-5.1` in the checked-in launch profile.
Choose a tool-capable model available to your OpenAI API project by editing
the `--model` argument in that local profile when necessary. The text-model
run **incurs OpenAI API usage charges**, potentially more than one attempt.
It does **not** trigger GPT Image, Gemini, TTS/STT, LTX or video rendering.

Success prints:

```json
{
  "status": "completed",
  "visual_plan": ".video_factory_work/agent_b_preview_phone/visual_plan.json",
  "block_count": 3
}
```

The same directory also contains `agent_b_project.json` and
`session.sqlite3`. The final `visual_plan.json` is the **only creative
output**, and Python revalidates it before freezing the project.

If no plan can pass in the bounded number of attempts, the preview fails
closed rather than generating unvalidated media.

## 6. Repeated tests, debugging and safety

- Reusing an already frozen project returns its saved plan rather than paying
  for another model generation. **Choose a new private project folder** for
  another independent run. Never reuse a project directory for a new script.
- If your instructions or B1.1 blocks change, the fingerprint prevents
  silent reuse; choose a fresh project directory.
- The preview has no cross-video narrative memory. Every project holds its
  own SQLite session and plan state.
- This phase does not claim to implement physical grids, image-reference
  chains, CTA downstream integration, B2.2 motion adaptation or MP4 output.
  Those are subsequent phases.
- The global logical A1/A2 sequence labels are already validated, but
  the current paid-image grid generator still needs its Phase 3 adapter.
- No concurrent writers to the same project folder are supported.
- A successful offline preflight and GitHub CI check **do not imply** that
  a real OpenAI model call has been executed. The VS Code paid planning test
  must be run with your own credentials to establish that evidence.

### Troubleshooting

- `No module named agents`: VS Code selected the wrong interpreter; switch
  to the `.venv-agent-b` environment and rerun setup.
- `OPENAI_API_KEY is missing`: fill in root `.env` and retry the real
  profile; preflight does not need a key.
- `Project script or instruction bundle changed`: use a **new** private
  project folder, not a session belonging to a different script.
- `No valid unified audiovisual plan`: inspect the tool errors and the
  project checkpoint. The controller intentionally rejects invalid plans.
- Debugger cannot find `scripts.pipeline`: open the **repository root** in
  VS Code; `launch.json` uses that folder as the working directory.

## CI verification

The regular Python CI validates the complete repository without the optional
SDK. A second workflow, **Agent B Preview SDK Smoke**, installs the pinned SDK
in its own `.venv-agent-b`, checks dependency consistency, runs the actual
preflight against the bundled example **without an API key**, and executes
the Agent B unit tests. That workflow does **not** use paid APIs.

See PR #336: https://github.com/yagobordell/Bordell-s-Flow/pull/336
