# B1.1 → B1.2 → B2 (replacement planning bots)

This standalone, opt-in stage runs the three user-supplied audited instruction documents
**byte-for-byte** from `src/ai_video_factory/bots/prompts/`. The expected SHA-256
checksums are embedded in `workflow.py` and verified before API calls.

- B1.1 runs once against the complete, unmodified script. The runtime resolves unique
  verbatim `first_words` / `last_words` anchors into contiguous block text and refuses any
  gap, duplicate, overlap, normalized text, or ambiguous anchor.
- B1.2 runs independently for each single materialized block. It emits 1A, 1B, ... 1AA,
  2A, ... IDs, preserves exact source characters, and enforces 40 source words per beat.
- B2 runs independently on each completed B1.2 block, preserves all upstream text,
  IDs and metadata, and assigns exactly one of `avatar`, `avatar_media`, `media_image`,
  `media_video` with the documented description/null rules.
- Per-block results are collected in ascending block ID, regardless of call completion
  order. B1.2 workers finish as a parallel wave before any B2 worker begins; B2 workers
  then run as a parallel wave. Both waves have the same bounded concurrency limit.

## Script library and selection

Keep any number of authoritative UTF-8 plain-text scripts in `data/input/scripts/`,
for example `historia_roma.txt` and `documental_japon.txt`. This directory is
present in the repository, but its contents are ignored by Git so local scripts are
not committed accidentally. Do not rename or copy a script to `script.txt`.
From the repository root, after setting `OPENAI_API_KEY` in local `.env`:

List the available scripts (no API key or inference is required for this command):

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --list-scripts
```

Start the pipeline without a script argument to choose from a numbered menu:

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py
```

For a non-interactive run, choose the file explicitly:

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py `
  --script documental_japon.txt --max-parallel-calls 8
```

`--script documental_japon` (without the `.txt` suffix) also works. To use
a different library directory, specify `--scripts-dir PATH`. A direct positional
file path is still accepted for existing automation, but the library is the
recommended input location. If no script is selected and the terminal is
non-interactive, the runner exits with instructions rather than selecting an
arbitrary script. A missing or invalid selection cannot start inference.

Each script gets its own default output directory,
`data/output/b_pipeline/<script-name>/`. A repeated run of the same script removes
**that script's previous output directory before the first API call**, then creates
a fresh run. Other script folders are not touched. The source `.txt` is never
deleted or rewritten. `--output PATH` overrides the **output root**, not the
per-script folder: the runner still appends `<script-name>/`. The runner refuses
to delete an unrelated directory, a symlink or a path containing the input script.
The script is read as UTF-8 without stripping or normalizing any characters.
B1.1 receives the selected complete text as `plain_script_for_recording`.

One finished script has the following structure (B1.2 and B2 repeat one subfolder
per frozen block):

```text
data/output/b_pipeline/historia_roma/
  .b_pipeline_run.json
  B1.1/
    input.json
    output.json
  B1.2/
    block_1/
      input.json
      output.json
    block_2/
      input.json
      output.json
  B2/
    block_1/
      input.json
      output.json
    block_2/
      input.json
      output.json
  visual_plan.json
  api_costs.json
```

Each `input.json` is the exact decoded payload sent to that bot, including the
single materialized block for B1.2 and the upstream beats for B2. Each
`output.json` contains the corresponding validated bot response. Inputs are
written before each request and outputs after stage validation; a failed run
can therefore leave diagnostic inputs and successfully completed outputs.

At the end, `api_costs.json` and the console report a USD estimate for B1.1,
B1.2, B2 and the whole run. It uses actual `response.usage` counts, prices
ordinary, cached and cache-write input separately, and includes reasoning tokens
in billed output. Standard GPT-6 Luna rates (USD per million text tokens,
2026-09-26): input $0.10, cached input $0.01, cache writes $0.125, output
$0.50; long-context requests above 272K input tokens use the documented
multipliers. The source is https://developers.openai.com/api/docs/pricing .
When API usage or verified pricing is missing, the total is `null`/unavailable
rather than silently reporting zero; a partial priced subtotal remains visible.
This is a token-based estimate, not the final invoice. Failed runs also save
the available partial usage.

All three bots use `OPENAI_B_MODEL=gpt-6-luna` and
`OPENAI_B_REASONING_EFFORT=medium` (both are independently configurable). The CLI
explicitly sets the OpenAI provider to Standard/default service tier. It uses
Responses API Structured Outputs; therefore the instruction documents' requested
Markdown code fence is replaced by the schema transport, with all semantic and
field-level rules unchanged.

Outputs are grouped under the script's `B1.1/`, `B1.2/block_<id>/` and
`B2/block_<id>/` directories, with an application-owned `visual_plan.json` join
and a separate `api_costs.json` ledger. No legacy phase2 artifact is changed.

The old bot implementations live only in `src/ai_video_factory/legacy_bots/` for
compatibility with the existing phase3–9 production path. The active
`src/ai_video_factory/bots/` package contains only the replacement B1.1/B1.2/B2
components. Existing legacy imports are redirected to `legacy_bots` until the
downstream pipeline is migrated.

**No production cutover:** the existing phase3–9 pipeline expects integer Beat IDs,
scene/shot groupings, and shot-based keyframes/video. B1.2 emits string Beat IDs and
B2 chooses avatar / still / video per beat. Feeding this visual plan into the old
pipeline via a lossy adapter would silently discard the audited contracts. An
explicit downstream schema, timeline, composition and caching migration is required
and should pass end-to-end tests before enabling it in `run_video_factory.ps1`.

The validations here establish structural fidelity, source reconstruction, 40-word
limits, type/ID preservation, and mandatory intro/close avatars. Whether a media
brief is *semantically* faithful or physically feasible still needs representative
manual or model-based evaluation; schemas alone cannot prove that.
