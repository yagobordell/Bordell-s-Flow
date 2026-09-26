# B1.1 → B1.2 → B2 (replacement planning bots)

This standalone, opt-in stage loads its instruction documents from
`src/ai_video_factory/bots/prompts/` on every run. Prompt edits take effect immediately;
the runner does not require checksum updates. A transport note for Responses API
Structured Outputs is appended at runtime.

- B1.1 runs once against the complete, unmodified script. The runtime resolves unique
  verbatim `first_words` / `last_words` anchors into contiguous block text and refuses any
  gap, duplicate, overlap, normalized text, or ambiguous anchor.
- B1.2 runs independently for each single materialized block. It emits 1A, 1B, ... 1AA,
  2A, ... IDs and enforces 40 words per beat. Validation ignores whitespace and punctuation
  differences when checking that the ordered beat text matches the block; other character
  changes remain errors.
- B2 runs independently on each completed B1.2 block, preserves all upstream text,
  IDs and metadata, and assigns exactly one of `avatar`, `avatar_media`, `media_image`,
  `media_video` with the documented description/null rules.
- Per-block results are collected in ascending block ID, regardless of call completion
  order. B1.2 workers finish as a parallel wave before any B2 worker begins; B2 workers
  then run as a parallel wave. Both waves have the same bounded concurrency limit.

## Script library, avatar selection and snapshot

Keep any number of authoritative UTF-8 plain-text scripts directly in
`data/input/`, for example `historia_roma.txt` and `documental_japon.txt`.
The input files are ignored by Git. There are no committed `.gitkeep`
placeholders: on a clean clone, the B runner creates `data/input/` when listing
or selecting scripts. To migrate previously ignored local files, move your
`data/input/scripts/*.txt` files into `data/input/` without overwriting
same-named files; the runner does not delete or move source scripts. Do not rename
or copy a script to `script.txt`.
Place one or more real PNG avatar images directly in `data/avatar/`, for
example `monje.png` and `maestra.png`. The folder includes a versioned
README, but **PNG images are not committed**. A missing avatar folder is
created by the runner. On an ordinary run, the script is selected first
and the avatar is selected second. The avatar is mandatory even if a
particular B2 result does not use it in every beat.

From the repository root, after setting `OPENAI_API_KEY` in local `.env`:

List the available scripts (no API key or inference is required for this command):

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --list-scripts
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --list-avatars
```

Start the pipeline without selection arguments to choose the script and then
the avatar from two numbered menus:

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py
```

For a non-interactive run, choose the file explicitly:

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py `
  --script documental_japon.txt --avatar monje.png --max-parallel-calls 8
```

`--script documental_japon` and `--avatar monje` work without their suffixes.
Use `--scripts-dir PATH` or `--avatars-dir PATH` for a different library;
a direct positional script path is still accepted. `--list-scripts` and
`--list-avatars` require no API key or inference. In a non-interactive
terminal, **both** script and avatar must be specified explicitly. Unknown,
missing, symlinked or invalid PNG avatars cannot trigger API calls or delete
the previous run output. The selected image is checked by Pillow before the
output folder is replaced.

To rerun only a later stage, use `--from B1.2` or `--from B2`. The runner
loads the prior output from `data/output/<script-name>/`, verifies that the
script SHA-256 matches, validates the saved B1.1 output, and for `--from B2`
also validates the complete B1.2 merge. It skips API calls for earlier stages
and replaces outputs from the selected stage onward. The run report carries
forward prior timing and API usage. A checkpoint from another script or a
missing/incomplete upstream stage is rejected before outputs are changed.

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py `
  --script documental_japon.txt --avatar monje.png --from B1.2

uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py `
  --script documental_japon.txt --avatar monje.png --from B2
```

Each script gets its own default output directory,
`data/output/<script-name>/` (directly under the output root, without a `b_pipeline` subfolder). A repeated run of the same script removes
**that script's previous output directory before the first API call**, then creates
a fresh run. Other script folders are not touched. The source `.txt` is never
deleted or rewritten. `--output PATH` overrides the **output root**, not the
per-script folder: the runner still appends `<script-name>/`. The runner refuses
to delete an unrelated directory, a symlink or a path containing the input script.
The script is read as UTF-8 without stripping or normalizing any characters.
B1.1 receives the selected complete text as `plain_script_for_recording`.
The avatar is **not** added to B2's audited request or response schema:
the application stores one run-level avatar binding that downstream stages
will resolve for both `avatar` and `avatar_media` beats.

One finished script has the following structure (B1.2 and B2 repeat one subfolder
per frozen block):

```text
data/output/historia_roma/
  avatar.png
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
  images/
    manifest.json
    block_1/
      1B.json
      1B.png
      1C.json
      1C.png
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
visual join. It adds a top-level `avatar` object containing `filename`,
`source`, `file: "avatar.png"`, `sha256`, `width` and `height`.
The same immutable binding appears at `run_report.json → run.avatar`.
`avatar.png` is a byte-for-byte snapshot of the selected source, so later
edits to `data/avatar/` cannot change the image used for this run. Original
`B2/block_<id>/input.json` and `output.json` are unchanged; the top-level
avatar binding applies to beats with `visual_type` `avatar` or `avatar_media`.
Lip-sync/video generation is not implemented by this selection feature.

The runner prints exactly one combined time/cost line as soon as each stage
finishes: B1.1 immediately after its validated output, B1.2 after all parallel
blocks and its merged output, then B2 after all parallel blocks and its merged
output. Once the visual plan and final report are saved, it prints the run
total. Durations are monotonic wall-clock seconds; stage duration is **not**
the sum of simultaneous per-block calls. Every printed line is flushed
immediately so redirected or piped runs can follow progress.

`run_report.json` is the **only run-metrics file**. It contains `run`
(source filename and SHA-256, selected avatar snapshot and SHA-256,
selected model, reasoning effort, status),
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
`visual_plan.json` and the consolidated `run_report.json`. No previous phase artifacts are generated.

The previous planning bots and their one-command production orchestration have
been removed from this branch. Salad workers, Postgres/R2 inference, capacity
control, and independent GPU stage clients remain available. Those stage clients
still accept the old shot-based artifacts and are **not** a supported consumer
of B2's string beat IDs and four visual strategies. A versioned downstream
migration with audio alignment, per-beat visuals, avatar handling, timing and
composition tests is required before restoring end-to-end video production.
No lossy adapter or silent fallback to the retired bots is used.

The validations here establish structural fidelity, source-word order after ignoring
whitespace and punctuation, 40-word
limits, type/ID preservation, and mandatory intro/close avatars. Whether a media
brief is *semantically* faithful or physically feasible still needs representative
manual or model-based evaluation; schemas alone cannot prove that.


## AI33 Pro: imágenes a partir de las descripciones de B2

Al terminar los tres bots y guardar el output completo de B2 y
`visual_plan.json`, el runner genera **una imagen por beat con
`description` no vacía**. Envía la descripción original de B2 sin
transformaciones como campo `prompt`. Los beats `avatar` con
`description: null` se omiten; los `avatar_media`, `media_image` y
`media_video` con descripción generan un PNG estático. Esta etapa no
genera vídeo ni lipsync y no modifica los JSON del bot.

El proveedor es AI33 Pro / OpenSpeaker. Su API usa el encabezado
`xi-api-key` y los endpoints `POST /v1i/task/price`,
`POST /v1i/task/generate-image` y `GET /v1/task/{task_id}`.
La configuración en `.env` es:

```dotenv
AI33_API_KEY=
AI33_IMAGE_MODEL=gpt-image-2.5-flare
AI33_IMAGE_ASPECT_RATIO=16:9
AI33_IMAGE_RESOLUTION=1K
AI33_IMAGE_QUALITY=low
AI33_POLL_TIMEOUT_SECONDS=1800
AI33_POLL_INTERVAL_SECONDS=8
# El fallback oficial utiliza OPENAI_API_KEY definido más arriba.
OPENAI_IMAGE_FALLBACK_ENABLED=true
```

El modelo predeterminado Flare es el que se ha verificado generando
`1280×720` en formato PNG. Para utilizar Sunburst, cambia solo
`AI33_IMAGE_MODEL=gpt-image-2.5-sunburst`. Ambos modelos admiten
`16:9`, `1K` y `low` según el catálogo de la cuenta, aunque la
integración no ha ejecutado una generación real de Sunburst. No hay
necesidad de cambiar ninguna imagen o conexión de Salad.

Se crea `images/manifest.json`, más el PNG
`images/block_<id>/<beat_id>.png` y su fichero de estado
`images/block_<id>/<beat_id>.json`. El estado se persiste **antes** del
POST de generación y conserva el `task_id` inmediatamente después de
recibirlo. Un error de polling `429/502/503/504` se reintenta con
backoff; el tiempo máximo por tarea es 30 minutos por defecto, medidos
desde el envío original y conservados entre reanudaciones. Al vencer ese
plazo, se realiza una última consulta de estado. Si no hay respuesta
`done` y existe un `task_id` confirmado, el fallback
`OPENAI_IMAGE_FALLBACK_ENABLED=true` llama a
`POST https://api.openai.com/v1/images/generations` con el **mismo
modelo** Flare/Sunburst, la `description` original de B2, `low`,
`png` y `size=1280x720`. Son las dimensiones 16:9 más pequeñas que
cumplen el mínimo de píxeles, múltiplos de 16 y relación de aspecto
documentados para la API oficial:
https://developers.openai.com/api/docs/guides/image-generation

Si la respuesta del POST de AI33 se pierde, se detiene con estado
`submitting_unknown`: **no se activa el fallback ni se repite una
solicitud potencialmente cobrada**. Si se pierde la respuesta del POST
oficial, se guarda `openai_submitting_unknown` y también se bloquea
cualquier repetición automática. La configuración por defecto no
reintenta dos veces una solicitud de generación. La tarea original de
AI33 puede completarse después de que el fallback haya terminado y
generar un cargo adicional en ese proveedor: no se afirma que el
timeout cancele ni reembolse el trabajo de AI33.

Para recuperar una ejecución después de un timeout o error de descarga,
sin volver a llamar a OpenAI ni seleccionar otro avatar:

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt --images-only
```

Se verifica el SHA-256 del guion y la existencia de los artefactos B2
antes de recuperar cualquier tarea. Una ejecución completa nueva evita
borrar el output si existen tareas AI33 pendientes. Si se reejecuta B2
y cambia una descripción con una tarea previa del mismo beat, el runner
se detiene en vez de reutilizar una imagen cuyo prompt ya no coincide.

Los metadatos por beat y el manifiesto incluyen modelo, ID de tarea,
archivo local, SHA-256, dimensiones y `credit_cost` real que comunica
AI33; `provider_credit_cost` se conserva por separado. En fallback, el
artefacto queda marcado `provider: openai_official_fallback` con el
`task_id` original de AI33, `fallback_reason: ai33_timeout`,
`size: 1280x720` y, si la API lo incluye, `openai_usage`. Los
contadores `ai33_count` y `openai_fallback_count` aparecen en el
manifiesto y en el informe de ejecución. Los costes oficiales en USD
no se inventan: se conserva el uso devuelto por OpenAI y el gasto real
se consulta en su plataforma. Los créditos de un AI33 que haya
superado el plazo pueden seguir pendientes incluso si el fallback
terminó. Los créditos AI33 no se mezclan con las métricas USD de
OpenAI de B1.1/B1.2/B2.

Para iterar solo los bots sin crear imágenes, usa `--skip-images`.
Después ejecuta `--images-only` para generar lo pendiente a partir
del B2 guardado. Los clientes GPU existentes siguen siendo herramientas
independientes: esta etapa no activa Salad ni conecta todavía un vídeo
final al plan B2.
