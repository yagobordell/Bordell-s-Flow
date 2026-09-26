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
- If a B2 response fails output validation, that block is retried once with the
  validation error and rejected response included; rejected responses are saved under
  `B2/block_<id>/rejected_attempt_<n>.json` and retry calls are included in costs.
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


## BotFish por bloques + TTS de AI33 Pro

El documento `src/ai_video_factory/bots/prompts/fish_audio.md` define el director
de voz por bloques. B1.1 sigue recibiendo el guion UTF-8 completo y
materializa sus bloques exactos antes de iniciar BotFish. En cuanto termina
B1.1, BotFish se ejecuta **en paralelo con B1.2**, una llamada independiente
por bloque, usando exactamente el mismo contrato de entrada que B1.2:
`narrative_core`, `current_block_id` y `blocks` con el único bloque
materializado (`block_id`, `type`, `emotional_entry`, `emotional_exit`, `text`).
El director usa la metadata como contexto, pero solo devuelve
`plain_script_for_recording` del bloque seleccionado.

Cada respuesta se verifica **antes** de llamar al TTS. La regla del prompt
sigue siendo insertar exclusivamente etiquetas Fish, sin reescribir ni
normalizar el texto. El validador de seguridad tolera diferencias de
espacios y puntuación Unicode como el validador de B1.2; ninguna
sustitución, pérdida, adición o reordenación de letras, cifras u otros
caracteres léxicos llega a AI33. También se rechazan etiquetas partidas
dentro de palabras y etiquetas finales sin texto hablado.

Cada bloque validado crea su **propia** tarea pagada de voz usando la
misma voz Fish y `POST https://api.openspeaker.ai/v3/text-to-speech`.
Las llamadas están limitadas por `--max-parallel-calls` y se
almacenan de forma durable por bloque: input, respuesta dirigida,
estado de POST/polling, task ID y audio descargado. Los bloques
terminados no se vuelven a cobrar al reanudar. Un POST con resultado
incierto bloquea su reenvío automático; las demás tareas ya
iniciadas se esperan sin cancelarlas al producirse un error.

Una vez descargados **todos** los audios, FFmpeg decodifica cada pista
de origen a PCM WAV mono de 48 kHz. Se concatena por `block_id`
ascendente con **exactamente 500 ms de silencio entre bloques** (nunca
después del último). No se concatenan bytes MP3 ni se depende de los
retrasos variables de los codificadores de audio. La pista final,
`audio/runs/<run-id>/narration.wav`, se publica en `audio/latest.json`
solo al completar y verificar el ensamblado. Se conservan los hashes
y créditos de cada bloque y el hash de la pista final.

Los modos `--regenerate-audio` y `--resume-audio` reutilizan el
checkpoint validado `B1.1/output.json` y nunca repiten B1.1, B1.2, B2
ni imágenes. La reanudación compara el hash del guion, las entradas
por bloque, el prompt, la voz y la velocidad. Para ensamblar se necesita
`ffmpeg` en el `PATH`. `--no-audio` omite íntegramente la etapa de voz.
Las imágenes de OpenAI Batch siguen empezando al finalizar B2, sin esperar
a la pista de voz final. En cuanto están completos las imágenes,
la voz y el STT, se monta automáticamente `preview/static_preview.mp4`.
Este vídeo es un simulacro estático, sin animación ni sincronización labial.

```powershell
# Ejecutar planificación e imágenes sin voz:
.\run.ps1 test2 Jorge --no-audio
# Regenerar solo el audio, por bloques, desde B1.1 validado:
.\run.ps1 test2 --regenerate-audio
# Reanudar tareas conocidas sin duplicar POST de pago:
.\run.ps1 test2 --resume-audio
```
## STT por pista y montaje automático del simulacro

Cuando AI33 Pro entrega cada pista, se envía directamente a OpenAI
`whisper-1` con `response_format=verbose_json` y
`timestamp_granularities=[word]`. El resultado, sujeto al SHA-256 del
audio, se conserva como `audio/runs/<run-id>/blocks/block_<id>/stt.json`.
No se reenvía automáticamente una petición STT cuyo POST pueda haberse
cobrado: `stt_request.json` conserva ese estado para reconciliación.
El proveedor usa la clave `OPENAI_API_KEY` existente y las pistas de
voz se mantienen independientes durante la transcripción.

El WAV final se une por muestras PCM: se publican las posiciones y
duraciones de cada bloque calculadas a 48 kHz, con 24 000 muestras
exactas de silencio entre bloques. El compositor alinea las palabras
con los beats inmutables de B1.2/B2; si STT omite o modifica palabras,
detiene el montaje en vez de inventar cortes. La pista estática y su
`preview/timeline.json` se generan cuando terminan voz, STT e imágenes,
también al recuperar los últimos artefactos mediante `--images-only` o
`--resume-audio` si la otra mitad ya está lista. Se utiliza FFmpeg
+ FFprobe, vídeo H.264 1280×720 a 30 fps y audio AAC. `avatar_media` se
representa provisionalmente con avatar a la izquierda e imagen generada
a la derecha, 50/50; `media_video` usa su PNG estático. Los tiempos del
STT son estimaciones a nivel palabra y los cambios visuales se cuantizan
a fotogramas de 1/30 s. No se ejecutan animación, lip-sync ni llamadas
GPU para crear el simulacro. Para la API de STT:
https://developers.openai.com/api/docs/guides/speech-to-text

## GPT Image oficial: un batch independiente por imagen de B2

Al finalizar B2, por cada beat con `description` no vacía se prepara un
archivo `images/block_<id>/<beat_id>.batch.jsonl` con **una sola línea**
y una única solicitud `POST /v1/images/generations`, `n=1`. El runner
sube un archivo por beat y crea **un batch por archivo**; para N imágenes
se crean N archivos y N batches, nunca un batch agrupado. El prompt
conserva exactamente el prefijo `iphone 6 photo done by an elderly:  `
más la descripción original de B2, sin modificar su JSON. Los beats
`avatar` con `description: null` no generan nada. Esto genera PNG
estáticos, no lip-sync ni vídeo animado.

```dotenv
# La misma clave OPENAI_API_KEY usada por B1.1/B1.2/B2 y STT.
OPENAI_IMAGE_MODEL=gpt-image-2.5-flare
OPENAI_IMAGE_SIZE=1280x720
OPENAI_IMAGE_QUALITY=low
OPENAI_IMAGE_BATCH_TIMEOUT_SECONDS=1800
OPENAI_IMAGE_BATCH_POLL_INTERVAL_SECONDS=8
OPENAI_IMAGE_MAX_PARALLEL=4
```

Se admiten `gpt-image-2`, Flare y Sunburst. Los tres modelos usan PNG,
16:9, calidad low y 1280×720 tanto para el batch como para la generación
directa. El valor actual de `OPENAI_IMAGE_MODEL=gpt-image-2` se conserva
sin necesidad de modificar el `.env`. Cuatro beats pueden
ejecutarse simultáneamente (límite configurable de 1 a 16), pero
**cada batch sigue conteniendo exactamente una imagen**. El manifiesto
`images/manifest.json` lo escribe el coordinador en orden B2,
con los PNG y estados individuales `images/block_<id>/<beat_id>.json`.

La Batch API oficial exige `completion_window: "24h"`: el plazo de
**30 minutos es local por beat** y se conserva entre reanudaciones.
Si al llegar al plazo el batch no ha entregado su PNG, el runner
registra `batch_timed_out`, solicita una cancelación no bloqueante y
lanza una generación estándar de **ese mismo beat**. Un batch con fallo
terminal o respuesta inválida también usa generación directa individual.
La cancelación no garantiza que el batch no se cobre: puede acabar
durante el proceso de cancelación; el manifiesto advierte de ese riesgo.
Si la clave falta al intentar fallback, el estado permanece recuperable.

Cada estado conserva `input_file_id`, `batch_id`, timestamp original,
fingerprint del prompt efectivo, estado de cancelación y, cuando existe,
`openai_usage`. Se escribe `batch_creating_unknown` **antes** de crear
el batch y `direct_submitting_unknown` antes del POST de generación
estándar: si la respuesta de pago se pierde, `--images-only` **no**
duplica el POST ni inicia un segundo batch a ciegas. Una nueva subida
de un archivo tras una subida incierta no lanza generación de imágenes,
pues solo la creación posterior del batch inicia el trabajo.

```powershell
.\run.ps1 test2 --images-only
```

El runner verifica el SHA-256 del guion y los artefactos B2 antes de
reanudar. Los PNG y batches completados no se regeneran. Los estados
heredados de AI33 se protegen: no se borran ni convierten implícitamente
en nuevas solicitudes oficiales, para evitar duplicación de pagos.
Reconcílialos primero y utiliza una raíz de salida separada para una
nueva generación. AI33 se mantiene **solo** en la etapa de voz Fish.

Documentación oficial:
https://developers.openai.com/api/docs/guides/batch
https://developers.openai.com/api/docs/guides/image-generation

Para el uso habitual en PowerShell, desde la raíz del repositorio:
```powershell
.\run.ps1 test2 Jorge --no-image
```
El acceso directo establece la biblioteca `data/input/scripts/` y llama
al runner Python con `uv run --locked --extra dev`; los nombres del guion
y avatar admiten omitir `.txt` y `.png`. Si se omiten ambos, el runner
muestra los menús interactivos. Esta configuración del acceso directo no
cambia el valor por defecto de `--scripts-dir` en el runner original.

Para iterar solo los bots sin crear imágenes, usa `--no-image`
(alias compatible de `--skip-images`):

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt --avatar monje.png --no-image
```

El runner termina después de escribir B2 y `visual_plan.json`, sin llamar
a la API oficial de OpenAI para imágenes. Cuando quieras
generarlas, ejecuta `--images-only` a partir del B2 guardado. Los clientes GPU existentes siguen siendo herramientas
independientes: esta etapa no activa Salad ni conecta todavía un vídeo
final al plan B2.
