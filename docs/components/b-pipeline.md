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

Keep any number of authoritative UTF-8 plain-text scripts directly in
`data/input/`, for example `historia_roma.txt` and `documental_japon.txt`.
The input files are ignored by Git. There are no committed `.gitkeep`
placeholders: on a clean clone, the B runner creates `data/input/` when listing
or selecting scripts. To migrate previously ignored local files, move your
`data/input/scripts/*.txt` files into `data/input/` without overwriting
same-named files; the runner does not delete or move source scripts. Do not rename
or copy a script to `script.txt`.
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
`data/output/<script-name>/` (directly under the output root, without a `b_pipeline` subfolder). A repeated run of the same script removes
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
data/output/historia_roma/
  run_report.json
  B1.1/
    input.json
    output.json
  B1.2/
    merged_output.json
    block_1/
      input.json
      output.json
    block_2/
      input.json
      output.json
  B2/
    merged_output.json
    block_1/
      input.json
      output.json
    block_2/
      input.json
      output.json
  visual_plan.json
```

Each `input.json` is the exact decoded payload sent to that bot, including the
single materialized block for B1.2 and the upstream beats for B2. Each
`output.json` contains the corresponding validated bot response. Inputs are
written before each request and outputs after stage validation; a failed run
can therefore leave diagnostic inputs and successfully completed outputs.
The application-owned `B1.2/merged_output.json` and
`B2/merged_output.json` combine the validated blocks in ascending `block_id`
order without making a second model call or changing the original single-block
input/output contracts. `visual_plan.json` remains the application-owned
visual join.

The runner prints exactly one combined time/cost line as soon as each stage
finishes: B1.1 immediately after its validated output, B1.2 after all parallel
blocks and its merged output, then B2 after all parallel blocks and its merged
output. Once the visual plan and final report are saved, it prints the run
total. Durations are monotonic wall-clock seconds; stage duration is **not**
the sum of simultaneous per-block calls. Every printed line is flushed
immediately so redirected or piped runs can follow progress.

`run_report.json` is the **only run-metrics file**. It contains `run`
(source filename, SHA-256, selected model, reasoning effort, status),
`api_costs` (token-usage-derived USD costs per bot, full run, and each
request) and `timings` (wall-clock seconds per bot, full run, and each
request). The file is written at the start, updated after each completed
stage and finalized on success or failure; there are no separate
`api_costs.json`, `timings.json` or `.b_pipeline_run.json` files.
Standard GPT-6 Luna rates (USD per million text tokens, 2026-09-26):
ordinary input $0.10, cached input $0.01, cache writes $0.125, output
$0.50; long-context requests above 272K input tokens use the documented
multipliers. Source: https://developers.openai.com/api/docs/pricing .
When API usage or pricing is missing, the corresponding cost is marked
unavailable instead of silently reporting zero.


All three bots use `OPENAI_B_MODEL=gpt-6-luna` and
`OPENAI_B_REASONING_EFFORT=medium` (both are independently configurable). The CLI
explicitly sets the OpenAI provider to Standard/default service tier. It uses
Responses API Structured Outputs; therefore the instruction documents' requested
Markdown code fence is replaced by the schema transport, with all semantic and
field-level rules unchanged.

Outputs are grouped directly under `data/output/<script-name>/`, within the
`B1.1/`, `B1.2/block_<id>/` and `B2/block_<id>/` directories, with
`B1.2/merged_output.json`, `B2/merged_output.json`, the application-owned
`visual_plan.json` and the consolidated `run_report.json`. No legacy phase2
artifact is changed.

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
