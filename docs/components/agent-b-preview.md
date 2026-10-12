# Agent B standalone planning preview

This is an isolated implementation experiment. It does **not** replace B1.2/B2/B2.1
in the production runner, call paid image/video APIs, or change `run.ps1`.

## Prepare

The project environment is currently locked by `uv.lock`. To try the Agents
SDK independently, install `config/agent-b-preview-requirements.txt` in an
isolated virtual environment with compatible project dependencies. The preview
dependency **has not been added to the production lock**. Do not edit production
dependencies manually while testing.

Set `OPENAI_API_KEY` in that isolated environment. Choose the model explicitly.

## Input format

Create an input JSON file containing the unchanged B1.1 `narrative_core` and
a chronologically ordered array of **materialized** B1.1 source blocks.
Each block has exactly `block_id`, `type`, `emotional_entry`,
`emotional_exit`, and verbatim `text`.

```json
{
  "narrative_core": {
    "central_question": "What happened?",
    "final_answer": "The number was spoofed."
  },
  "blocks": [
    {
      "block_id": 1,
      "type": "intro",
      "emotional_entry": "tension",
      "emotional_exit": "suspicion",
      "text": "Your phone rings. You look at the screen."
    }
  ]
}
```

## Execute

```bash
python -m scripts.pipeline.run_agent_b_preview \
  --input path/to/materialized_b11.json \
  --project path/to/private_agent_b_project \
  --model YOUR_SUPPORTED_OPENAI_MODEL
```

The tool runtime can retrieve any B1.1 text range, query the protected
instruction documents, validate candidate plans in Python, save creative
per-block drafts for long scripts, and finalize one global visual plan.

Upon success, `PROJECT/visual_plan.json` is written and the project is
marked frozen. The SDK conversation is stored in `PROJECT/session.sqlite3`.

The same script with a different instruction-bundle hash cannot silently resume
an old project; use a different private project directory.

## Non-goals / gates

- The preview is **not** wired into the normal runner, CTA detection,
  voice/STT, image production, motion adaptation, LTX, or MP4 output.
- Full B1.1 `span` metadata / avatar reference and CTA-specific downstream
  fields will be provided and validated by the later pipeline adapter.
- Cross-block logical labels A1/A2 are checked by the Python plan validator,
  but physical multi-image grid generation is **not implemented** in this phase.
- Concurrent writers for one project are unsupported. The controller must
  serialize writes; multi-process locking belongs to the integration phase.
- A model call can incur API usage charges; the preview does not submit
  paid image/video jobs. No provider-backed run has been claimed or certified.
- The optional SDK dependency has not been reconciled with `uv.lock` yet;
  production integration will update and validate the lock together.
- Nothing merges into `main` without the normal review and CI checks.
