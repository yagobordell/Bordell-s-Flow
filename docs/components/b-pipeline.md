# B1.1 → B1.2 → B2 (replacement planning bots)

Professional audio is now a downstream **post-B2** path:
V1 (whole-film rhythm) → V2 (beat-level intent per block) →
V3 (verbatim delivery, sustained style and optional human vocal tags) →
Gemini 3.8 TTS per block. Casero retains its independent Fish/MiniMax/
ElevenLabs path. V1/V2/V3 do not alter B2 visual contracts or any source
narration. The original B8 documents are archived and the adapted prompts
are versioned in `bots/prompts/v{1,2,3}.md`.
See [Professional voice documentation](professional-voice.md).


This standalone, opt-in stage loads its instruction documents from
`src/ai_video_factory/bots/prompts/` on every run. Prompt edits take effect immediately;
the runner does not require checksum updates. A transport note for Responses API
Structured Outputs is appended at runtime.

- B1.1 runs once against the complete, unmodified script. The runtime resolves unique
  verbatim `first_words` / `last_words` anchors into contiguous block text and refuses any
  gap, duplicate, overlap, normalized text, or ambiguous anchor.
- B1.2 runs independently for each single materialized block. It emits 1A, 1B, ... 1AA,
  2A, ... IDs and enforces 40 words and 13–100 decoded characters per beat (excluding
  leading/trailing whitespace). A sole beat in a block of at most 12 stripped characters
  is the unavoidable exception because B1.2 cannot merge across blocks. Validation
  ignores whitespace and punctuation differences when checking the ordered beat text
  against the block; other character changes remain errors.
- B1.2 responses with beats over 100 or at most 12 characters (outside that exception)
  receive one automatic Responses API Structured Outputs correction call for that block.
  The model receives the original block, validation error and rejected response; it
  splits/merges only adjacent immutable source slices and renumbers beat IDs. The corrected
  response must reconstruct the original block character for character. Rejected responses
  are stored in `B1.2/block_<id>/rejected_attempt_<n>.json`, and corrections are included
  in B1.2 usage, cost and timing. If the correction is still invalid, the block fails
  without starting B2.
- B2 runs independently on each completed B1.2 block, preserves all upstream text,
  IDs and metadata, and assigns exactly one of `avatar`, `avatar_media`, `media_image`,
  `media_video` with the documented description/null rules.
- If a B2 response fails output validation, that block is retried once with the
  validation error and rejected response included; rejected responses are saved under
  `B2/block_<id>/rejected_attempt_<n>.json` and retry calls are included in costs.
- Per-block results are collected in ascending block ID, regardless of call completion
  order. B1.2 workers finish as a parallel wave before any B2 worker begins; B2 workers
  then run as a parallel wave. Both waves have the same bounded concurrency limit.

## Professional B2: demonstrator identity, animation prompts and speaking intent

The professional bot has two mandatory presenter-return points:
the **first** beat of an `intro` block and the **last** beat of a `close`
block reuse the original avatar image with `visual_type: "avatar"`,
`prompt: null`, `video_prompt: null`, `reference: false` and
`audio: true`. The professional output schema validates both assignments
and its automatic correction retry can repair a noncompliant output.
All other beats retain neutral visual choice.

The opt-in `profesional` bot chooses `visual_type` independently from casting:
a still or video can show the selected avatar demonstrating a presenter-led
example instead of an unrelated generic person. The preference applies
where participation feels natural, including suitable second-person advice,
but established third-party characters and genuinely identity-free product
details are not recast as the avatar.

For `reference: true` prompts showing a referenced person's **visible face**,
B2 describes the intended expression and relevant gaze/attention for that
specific scene. Identity is preserved without copying the source photo's fixed
expression, pose, background or lighting. Object-only and hands-only prompts
do not invent facial-expression details. Wardrobe and setting are preserved
within a continuous occasion only when continuity calls for them.

Professional B2 makes no special branding or packaging choices: the scene
description is driven by the beat, not by brand-specific positive or negative
instructions. The professional grid adds no such rules. The professional bot's
general scene-prompt instructions require affirmative scene descriptions
instead of generated negative clauses, exclusions or stock disclaimers.
When a scene includes a phone, screen or document, B2 freely chooses
the scene and camera viewpoint that best communicates the beat. A person
using the device or document is optional: object-only close-ups and other
source-grounded compositions are equally valid. The specific
app/page/document content stays in the same brief `(For context: ...)`
note, not in instructions to render visible text or exact interfaces.
This applies to still-image and video-animation prompts. Professional
grid requests preserve the existing Voice over context unchanged and
honor each panel's chosen composition without adding a person or forcing
a device/document orientation. The legacy `casero` prompt and grid
are unchanged.

A clearly visible presenter making a simple, speaking-compatible
demonstration can use `audio: true`, including on a `media_video` beat;
the current LTX grouping routes that beat to A2V with the narration audio.
A2V currently has a fixed speaking-to-camera prompt, so its suitability
for detailed independent hand/action animation is limited. Choose
`audio: false` with observational I2V/voice-over when the movement itself
must be shown accurately, the mouth is obstructed, or the face is cropped
out. These are editorial prompt instructions, not a deterministic
computer-vision validator of generated facial expressions.

A professional beat now adds a dedicated `video_prompt` field alongside
`prompt`, `reference`, `audio` and `visual_type`. For `media_video`,
`prompt` describes one motion-ready starting **image**, while the required
`video_prompt` describes how the action develops during the resulting video:
natural gestures, hand/object interaction, gaze, framing and realistic pacing.
For `avatar`, `avatar_media` and `media_image`, `video_prompt` is exactly
`null`. Referenced presenter demonstrations with visible practical action
default to `media_video`; static portraits, pauses and completed results can
still use `media_image`. A narrow schema guard rejects clearly active
referenced-presenter still prompts and triggers the existing correction retry.

For `media_video + audio:false`, the planner sends `video_prompt` verbatim
as LTX I2V's animation prompt, independently of the image-generation prompt.
This voice-over route should show the action naturally, not a presenter
addressing the camera without synchronized speech. For
`media_video + audio:true`, the planner retains the code-owned canonical
speaking-to-camera prompt and augments it with `video_prompt`, submitting
the beat's real speech audio to A2V. A2V remains speaking-focused and
does not guarantee elaborate secondary actions. New plans keep the motion
field in the aligned timeline and video run fingerprint, so changing it
generates a different paid request and never silently reuses a clip created
with an older prompt. Historical saved plans without the field retain the
old generic I2V/A2V behavior when resumed.

The professional image grid uses only `prompt` to create its still key
images; it never feeds `video_prompt` to the image model. The legacy
`casero` grid remains unchanged.

The professional bot also has **global shot-framing freedom** for every
generated beat, not only numbered sequences: it deliberately chooses useful
shot distance, angle, crop and focus (including tighter close-ups and a
slightly zoomed-in still composition). This changes the generated starting
image's composition, not A2V camera animation; downstream Ken Burns still
governs actual zoom movement for static `media_image` shots. For contiguous
numbered steps, B2 applies a stronger shot-progression check so the same
person and setting stay continuous while neighboring images emphasize
different framing/details instead of duplicating nearly identical views.
The professional image-grid prompt explicitly preserves each panel's
distinct shot composition rather than normalizing all panels to one angle.
Because B2 workers operate one block at a time, it uses the global framing
rule everywhere but can directly compare adjacent prompts only within
each block, not across unseen neighboring blocks.

**Strict professional audio/type contract:** `media_image` MUST have
`audio: false` (ordinary master narration may still play as voice-over).
Generated full-screen synchronized speech must use `media_video` with
`audio: true` and is routed through A2V; `media_video` with
`audio: false` uses observational I2V. The professional Pydantic model
rejects invalid speaking stills, allowing the existing automatic B2
schema-correction retry to request a corrected result. Video planning
also rejects legacy saved plans containing `media_image + audio:true`
instead of accidentally treating those stills as speaking videos.
`avatar` and `avatar_media` continue to require `audio: true`.
The `casero` bot's output contract and grid prompt are unchanged.

The code-owned high-end phone-photography style remains separate from B2:
`effective_image_prompt()` injects it during generation, after any
numeric continuity marker.

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
**that script's previous output directory before the first planning API call**, then creates
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
  qr.png
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

### Persistent ebook QR per avatar (local, no GPT calls)

On the first ordinary run with an avatar without a QR, the CLI requests the
ebook sales URL. Paste the complete HTTP(S) URL and press Enter. The runner uses
the local `segno` Python package to encode **exactly that URL** into a standard
black-on-white PNG with a four-module quiet zone and integer-size modules.
Generating the QR does not use GPT, OpenAI's image API, an external service or
paid credits. This does not affect the existing OpenAI planning and B2 image
generation stages.

The QR and destination are saved under
`data/avatar/.qr/<avatar-filename>/qr.png` and `qr.json` (ignored by Git).
Subsequent runs reuse the asset for the same avatar without prompting. Every
ordinary run snapshots the QR as `data/output/<script>/qr.png`, with the exact
URL, PNG SHA-256 and `generator: "segno"` in `run_report.json → run.avatar.qr`
and `visual_plan.json → avatar.qr`. The QR is prepared for future composition;
this stage does **not** place the QR on a video.

An existing GPT-generated QR is **never reused**: when its old metadata contains
the sales URL, the next run automatically replaces its PNG and metadata with a
Segno QR, without asking for the URL again. Old `request_pending.json` markers
can also be resolved locally from their saved URLs. If the stored URL is missing
or invalid, the runner stops for manual review instead of inventing a
destination. Existing Segno QR assets with damaged PNGs or mismatched hashes
are regenerated from their saved URL. If `--qr-url` differs from the saved URL,
the runner refuses to silently change the destination.

For a first non-interactive run, pass `--qr-url https://example.com/ebook`
along with `--script` and `--avatar`. To generate or refresh **only** the QR
without a guion, OpenAI key or calls to the rest of the pipeline, run:

```powershell
.\run.ps1 --qr-only --avatar monje --qr-url "https://example.com/ebook"
```

CI uses the independent ZBar decoder to verify that generated PNGs decode to
the exact URL (including query parameters). For final publication, check the
actual sales page on a phone and avoid shrinking or cropping the quiet zone.

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

GPT Image 2.5 Flare/Sunburst costs are included in the **same**
`run_report.json → api_costs` ledger as stage `B2 images`. Direct
Image API generations and edits are priced from each completed image
state's persisted `artifact.openai_usage`, not from an assumed fixed
price per image: USD per million tokens (2026-09-29) are $5 text input,
$8 image input and $30 image output. Both models use those token rates,
but their actual token counts may differ. Grid generation is charged
per API request, not per cropped panel. The report preserves the
breakdown per image, the stage subtotal and the combined run total.
`--images-only` reconciles the saved request states, including archived
images replaced by a later paid generation, without charging cached
responses twice. A missing usage breakdown or unknown POST outcome
leaves the complete total unavailable while still showing the known
subtotal. The figures are estimates from returned usage, not an invoice;
OpenAI STT and Fish TTS are excluded. Source:
https://developers.openai.com/api/docs/guides/image-generation .


B1.1, B1.2, B2 and the Fish director use the **same** text model
`OPENAI_MODEL=gpt-6-luna` and `OPENAI_REASONING_EFFORT=medium`.
There are no separate `OPENAI_B_MODEL` or `OPENAI_B_REASONING_EFFORT`
settings. The CLI explicitly sets the OpenAI provider to Standard/default
service tier. It uses
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
whitespace and punctuation, 40-word and 13–100-character
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
Este vídeo es un simulacro de montaje: los beats `media_image` tienen
zoom suave continuo; no incluye todavía sincronización labial. El nombre
`static_preview.mp4` se mantiene por compatibilidad.

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
a la derecha, 50/50; `media_video` usa su PNG estático. Los beats
`media_image` se animan localmente con FFmpeg mediante zoom in/out
centrado o Ken Burns in/out anclado en cada una de las cuatro esquinas.
Los estilos se alternan de forma determinista, con un zoom pequeño
(1.035–1.115), durante toda la duración del beat. Ni las imágenes fuente
ni sus recortes se modifican. `avatar`, `avatar_media` y `media_video`
mantienen sus fotogramas actuales. Los tiempos del STT son estimaciones
a nivel palabra y los cambios visuales se cuantizan a fotogramas de
1/30 s. Un `media_image` de menos de dos fotogramas falla explícitamente
antes de publicar un preview fijo. El movimiento queda registrado en
`preview/timeline.json`; no se ejecutan lip-sync ni llamadas GPU para
crear el simulacro. Para la API de STT:
https://developers.openai.com/api/docs/guides/speech-to-text

## GPT Image oficial: una solicitud estándar por imagen de B2

Al finalizar B2, por cada beat que requiere una imagen se prepara una
solicitud estándar `POST /v1/images/generations`, `n=1`. Para una imagen
con referencia de avatar se usa `POST /v1/images/edits` con la referencia
verificada. No se crean archivos JSONL ni trabajos en Batch API.

El estilo se añade **en código al construir la solicitud de imagen**, no dentro
del prompt de B2. El modo `casero` conserva el prefijo histórico
`iphone 6 photo done by an elderly:  `. El modo `profesional` recibe de B2
solo la descripción documental de la escena y `effective_image_prompt()`
antepone:

`Render the following scene as authentic high-end iPhone 16/17 Pro Max photography, not DSLR or cinematic photography.  instagram rhode style. Preserve the described scene, composition, relationships, lighting, and mood, realistic skin texture, natural facial imperfections, authentic everyday environment, documentary feel, not polished, not cinematic, not luxury editorial.`

Cuando B2 marca continuidad con `1. `, `2. `, etc., el número permanece como
primer token y el prefijo se inserta inmediatamente después. Los beats
`avatar` reutilizados con prompt nulo no generan imagen. Esto genera PNG
estáticos, no lip-sync ni vídeo animado.

```dotenv
# La misma clave OPENAI_API_KEY usada por B1.1/B1.2/B2 y STT.
OPENAI_IMAGE_MODEL=gpt-image-2.5-flare
OPENAI_IMAGE_SIZE=1280x720
OPENAI_IMAGE_QUALITY=low
```

El pipeline utiliza **GPT Image 2.5**, con el ID `gpt-image-2.5-flare`
por defecto o `gpt-image-2.5-sunburst` si se selecciona explícitamente.
`gpt-image-2.5` sin variante no es un identificador válido para la API.
La generación individual usa calidad `low` por defecto, incluida la
imagen inicial destinada a vídeo (1920×1088 en Profesional). Solo pasa a
`medium` cuando su prompt contiene una variante de `(For context`
(independientemente de mayúsculas, espacios, dos puntos o cierre de
paréntesis). **Todas las rejillas usan `medium`.** El campo global
`quality=low` del manifiesto indica el valor por defecto; la calidad
real se registra por solicitud y forma parte del fingerprint que impide
repetir inadvertidamente generaciones de pago. Actualiza también
`config/app.env` si todavía contiene `OPENAI_IMAGE_MODEL=gpt-image-2`;
un `git pull` no edita ese archivo privado.

Las rejillas 16:9 solo de imágenes estáticas generan paneles nominales
de 1280×720: 2×2 (2560×1440), 3×2 (3840×1440) y 3×3
(3840×2160). Si incluyen al menos un panel para vídeo, las 2×2 pasan
a 3840×2160 para paneles nominales de 1920×1080. Las 3×2 y 3×3
ya están en el tamaño máximo admitido y conservan paneles de 1280×720.
Los grids exclusivos `avatar_media` mantienen su geometría 8:9.
**Nunca se regenera un panel de rejilla con GPT**: los paneles de vídeo
se usan directamente a la resolución obtenida del recorte, sin
reescalado posterior. Solo los paneles estáticos profesionales reciben
el upscaler 2× previsto. Las generaciones independientes se ejecutan en paralelo según la
cantidad de jobs; `ImageOptions.max_parallel_images` permite
establecer un límite explícito cuando se invoca el proveedor.
Cada solicitud contiene exactamente una imagen. El manifiesto
`images/manifest.json` lo escribe el coordinador en orden B2,
con los PNG y estados individuales `images/block_<id>/<beat_id>.json`.

Cada estado conserva el fingerprint del prompt efectivo y, cuando existe,
`openai_usage`. Se escribe `direct_submitting_unknown` **antes** del POST:
si la respuesta de pago se pierde, `--images-only` no duplica la solicitud.

```powershell
.\run.ps1 test2 --images-only
```

El runner verifica el SHA-256 del guion y los artefactos B2 antes de
reanudar. Los PNG completados no se regeneran. Los estados
heredados de AI33 o Batch se protegen en `--images-only`: no se convierten
implícitamente en nuevas solicitudes oficiales, para evitar duplicación de
pagos. Reconcílialos primero antes de reanudar imágenes. Una ejecución completa
desde B1.1 elimina y recrea la carpeta anterior del guion; esto no cancela
trabajos remotos que ya se hayan enviado. AI33 se mantiene **solo** en la etapa
de voz Fish.

Documentación oficial:
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
Una ejecución completa borra y sustituye `data/output/<guion>` antes de
generarlo de nuevo.

Para iterar solo los bots sin crear imágenes, usa `--no-image`
(alias compatible de `--skip-images`):

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt --avatar monje.png --no-image
```

El runner termina después de escribir B2 y `visual_plan.json`, sin llamar
a la API oficial de OpenAI para imágenes. Cuando quieras
generarlas, ejecuta `--images-only` a partir del B2 guardado. Cuando los PNG,
Fish/STT y Postgres/R2 están listos, el runner encola automáticamente los beats
que requieren LTX. El controlador de capacidad de Salad sigue siendo un servicio
independiente: el runner no despliega GPUs.


## Integración LTX para B2 (I2V y A2V agrupado)

El runner inicia LTX después de terminar los PNG de B2 y la alineación de
palabras Fish/STT siempre que `POSTGRES_DSN` y R2 estén configurados. El
controlador de capacidad Salad y los workers de inferencia deben estar activos
por separado. Sin credenciales de transporte, sigue disponible
`preview/static_preview.mp4` y `video_generation` queda `deferred`.
Para generar **únicamente el preview tradicional** (`preview/static_preview.mp4`),
sin enviar trabajos I2V/A2V a Salad, usa `--no-video`. El runner conserva
la generación habitual de imágenes, Fish Audio y STT.

- Cada `media_video` con `audio:false` genera **un I2V independiente**:
  PNG de B2 como start image y el campo `video_prompt` profesional como
  instrucción de movimiento, con la duración del beat en la timeline STT.
  El generador ajusta la petición al grid temporal `8k+1` requerido por LTX.
  Los planes históricos sin `video_prompt` conservan la descripción genérica
  utilizada anteriormente. Un `media_video + audio:true` sigue la ruta A2V
  con WAV real de ese beat, no I2V.
- Los beats `avatar` y `avatar_media` consecutivos, también a través de
  límites entre bloques, forman **un solo A2V**. El runner recorta un único
  WAV del `narration.wav` original para todo el grupo. El recorte incluye
  los silencios de 500 ms entre bloques si están dentro del mismo grupo.
  Un beat `media_video` o `media_image` termina el grupo de avatar.
- El prompt positivo para `avatar` y `avatar_media` continúa siendo
  exactamente `A Person speaking to the camera.`. En los beats profesionales
  `media_video + audio:true`, A2V mantiene ese prefijo y concatena
  `video_prompt` para describir los gestos específicos compatibles con el
  habla. El perfil static-reference conserva el negativo fijo
  `camera zoom, zoom in, push in, dolly in, camera drift, camera shake, reframing, crop change, handheld camera, unintended camera movement`.
  El campo de imagen `prompt` no sustituye al prompt positivo de A2V.
- `avatar_media` conserva la imagen del beat en la mitad derecha y el avatar
  **animado** en la izquierda. `media_image` mantiene zoom/Ken Burns.
- El worker A2V admite un máximo de 1024 fotogramas crudos. Si un grupo
  consecutivo supera ese límite, el preflight falla antes de enviar trabajos
  pagados, en lugar de dividirlo y romper la continuidad.

El compositor transcodifica los beats a 24 fps, concatena los vídeos y
**descarta los audios embebidos de LTX**. El mux final usa únicamente el WAV
maestro Fish original, para que no se duplique el audio. La salida se guarda
en `video/runs/<fingerprint>/final.mp4`, con el manifiesto de generación,
hashes y timeline de cada beat. `video/latest.json` y
`run_report.json -> run.video_generation` apuntan al resultado.

Cada tarea utiliza IDs y huellas inmutables en Postgres y artefactos R2.
Si se interrumpe una ejecución, no se envía una solicitud pagada distinta
para una tarea ya registrada; un manifiesto incompleto bloquea borrar la
carpeta y comenzar un nuevo B1.1. Estos son los dos modos de ejecución
desde Windows (ejemplo: guion `test2.txt` y avatar `Jorge.png`):

```powershell
# B1/B2, Fish, imágenes y preview estático. No solicita ningún trabajo LTX.
.\run.ps1 test2 Jorge --no-video

# Cuando el preview ya existe, genera o reanuda solo el vídeo final con LTX.
.\run.ps1 test2 --generate-video
```

`--generate-video` exige un preview completo y válido, junto con el mismo
guion original, el plan B2, las imágenes y Fish/STT guardados. Comprueba el
SHA-256 del preview antes de enviar trabajos GPU. Reutiliza los artefactos
existentes y **no repite B1/B2, OpenAI Images, Fish ni el preview**.
La antigua opción `--videos-only` continúa funcionando como alias.

**Salad no arranca directamente desde `run.ps1`.** El Capacity Controller
único es el responsable de arrancar y detener los contenedores I2V/A2V según
la demanda en Postgres, incluso cuando se reanuda un vídeo. Antes de publicar
trabajo nuevo, el runner comprueba que este controlador tenga heartbeat y
reconciliación recientes; si no está operativo, falla con instrucciones antes
de subir inputs o crear nuevos jobs.

Cuando el controlador está sano, el runner prepara los inputs y publica **todo
el backlog LTX no cacheado en Postgres antes de esperar el primer resultado**.
Los workers de cada Container Group consumen esa cola compartida dinámicamente:
no existen bloques estáticos de clips asignados a una GPU. Esto permite que dos
réplicas se repartan 31 clips aproximadamente 16/15 y que una GPU que termine
antes tome el siguiente trabajo disponible.

I2V y A2V continúan siendo pools separados. Cada pool LTX solicita por defecto
una réplica por cada 30 jobs activos (`ceil(jobs/30)`), limitado por su
`max_replicas` y por `SALAD_AUTOSCALER_PROJECT_MAX_REPLICAS`. Los clips LTX
ya terminados y verificados pueden reutilizarse sin enviar trabajos GPU.

Comprueba el controlador desde la raíz del repositorio (sin solicitar GPU):

```powershell
uv run --locked --extra dev python scripts/salad/run_salad_capacity_controller.py --check-health
```

Si no existe un líder saludable, arranca **una sola instancia duradera** en una
terminal o servicio independiente, con `config/salad.env` configurado
(`SALAD_AUTOSCALER_ENABLED=true`, `SALAD_AUTOSCALER_PROJECT_MAX_REPLICAS`,
`SALAD_ORGANIZATION`, `SALAD_PROJECT`, `SALAD_API_KEY`,
`SALAD_CAPACITY_CONTROLLER_POSTGRES_DSN` Direct/Session y Postgres/R2):

```powershell
uv run --locked --extra dev python scripts/salad/run_salad_capacity_controller.py --poll-seconds 15
```

No arranques ni detengas manualmente los Container Groups mientras el
controlador sea el propietario de la capacidad. Si tu `POSTGRES_DSN` utiliza un
pooler transaccional, configura el DSN de sesión independiente del controlador
antes de ejecutar `--generate-video`.
Si los guiones están en `data/input/` en vez de `data/input/scripts/`,
usa directamente:

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script test2 --no-video --avatar Jorge
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script test2 --generate-video
```

`--images-only` y `--resume-audio` también activan LTX cuando la otra
mitad de artefactos está disponible. Las comprobaciones automáticas son
técnicas y no acreditan visualmente la sincronización de labios o la
inmovilidad de cámara; se requiere revisión de una generación real en Salad.

## Organización automática de guiones por avatar (nuevo `run.ps1`)

El acceso directo `run.ps1` usa ahora **data/ como punto de entrada**.
Para estrenar un avatar, coloca únicamente su PNG y el primer guion TXT
directamente dentro de `data/`. Al iniciar una ejecución, el programa
organiza los archivos automáticamente:

```text
data/
  Jorge/
    Jorge.png                      # Identidad visual del avatar
    meta/
      qr.png                       # QR persistente para todos los guiones
      qr.json
      voice.json                   # Configuración/Voice ID
      referencia.wav               # Si existe una grabación de clonación
      style.json                   # Estilo fijo: casero o profesional
    test02.txt                     # Guion nuevo PENDIENTE, todavía no ejecutado
    test01/                        # Guion ya INICIADO
      test01.txt                   # Guion original, byte por byte
      outputs/
        B1.1/                      # Inputs, outputs y checkpoints de cada bot
        B1.2/
        B2/
        visual_plan.json
        run_report.json
        avatar_voice.json
      media/                       # Cada etapa aparece AL TERMINAR, sin esperar a LTX
        images/
          avatar.png
          qr.png
          block_1/1B.png
          manifest.json           # Hashes y estado de las imágenes
        audio/
          narracion.wav            # Audio activo, incluso tras --regenerate-audio
          bloques/bloque_1.wav
          manifest.json           # Solo 'completed' tras verificar WAV y bloques
        preview/
          preview.mp4              # Vídeo estático de imágenes cuando esté listo
          timeline.json
          manifest.json
      test01.mp4                   # LTX verificado, sin esperar a copia global                   # Vídeo final, DIRECTAMENTE en esta carpeta
      manifest.json               # Solo presente tras una publicación completa
```

La carpeta `meta/` se crea automáticamente durante la incorporación del
avatar, antes de generar contenido. Guarda los metadatos persistentes del
QR y de la voz, junto con el audio de referencia si hay clonación.
En el primer guion se guarda además `meta/style.json` con el estilo visual
B2 seleccionado (`casero` o `profesional`). Los siguientes guiones de
ese avatar usan exactamente ese estilo sin volver a preguntarlo. Pasar
`--style` con otro valor produce un error explícito; los archivos
`meta/` no se sustituyen silenciosamente.

Después del primer guion, añade los siguientes TXT pendientes **sueltos
dentro de `data/Jorge/`**. El programa distingue automáticamente entre
TXT sueltos (pendientes) y carpetas con el mismo nombre (iniciados). Al
arrancar un guion pendiente lo mueve a `data/Jorge/<guion>/<guion>.txt`;
**no** mueve ni modifica los otros TXT pendientes. Las carpetas `outputs/` y `media/` se publican **etapa por etapa**:
el audio y sus bloques se hacen visibles al terminar Fish, las imágenes
se copian conforme aparecen en sus manifiestos, la vista previa estática
se publica al completarse y el MP4 LTX se guarda en la raíz del guion.
No hay que esperar al vídeo final para ver el audio generado. Cada
subcarpeta de `media/` incorpora su propio `manifest.json`, con estado
y hashes. El `outputs/run_report.json` visible contiene rutas relativas a
la carpeta del guion (por ejemplo `media/audio/narracion.wav`), **nunca
rutas internas presentadas como si estuvieran en `outputs/`**.
El `manifest.json` de la raíz solo aparece cuando se publica y se verifica
el resultado completo.
Una ejecución interrumpida se reanuda desde su carpeta, no vuelve a tratarse
como un guion pendiente. Los nuevos nombres de guion no reemplazan resultados
anteriores; se rechaza una generación nueva con un nombre que ya tenga carpeta.

```powershell
# Primera vez: data/Jorge.png y data/test01.txt.
.\run.ps1 test01 Jorge

# Después, añade data/Jorge/test02.txt.
.\run.ps1 test02 Jorge --no-video

# Reanudar test02 desde un vídeo previo validado:
.\run.ps1 test02 Jorge --generate-video

# Recuperar SOLO los archivos ya generados (audio, imágenes, preview o vídeo).
# Sin llamadas a OpenAI, Fish o Salad:
.\run.ps1 test01 Jorge --sync-only

# Publicar y verificar el resultado LTX completo, si ya existe:
.\run.ps1 test01 Jorge --publish-only
```

El sistema conserva **todos** los checkpoints, registros de trabajos pagados
y manifiestos originales en la carpeta interna
`.video_factory_work/<avatar>/<guion>/`, independiente del árbol visible.
Por ejemplo, una narración regenerada de Corea podría conservarse en
`.video_factory_work/corea/corea/audio/runs/<ID>/narration_audio_regen_1.wav`,
pero su ubicación **visible** es
`data/corea/corea/media/audio/narracion.wav`.
Los audios de bloques aparecen en `media/audio/bloques/` y cada etapa
publicada tiene su propio manifiesto de integridad.

No borres `.video_factory_work` si necesitas reanudar una ejecución. Si
un trabajo anterior generó medios internos pero no llegó a copiarlos a
`data/`, utiliza `--sync-only`: solo publica los archivos que
**efectivamente existen y superan SHA-256**. Si faltan los originales
internos, informa del error; un informe JSON, por sí solo, no puede
restaurar archivos que ya no existen. El publicador
comprueba el guion, los hashes de imágenes y audio y el MP4 antes de crear el
`manifest.json` final. La estructura publicada contiene solo los recursos
elegidos por los manifiestos actuales, no generaciones descartadas. Publicar
dos veces el mismo resultado verifica y reutiliza los archivos ya presentes;
si un archivo publicado fue modificado o falta, falla sin regenerar ni
sobrescribirlo. Se conserva `--legacy-layout` para ejecutar directamente
el runner previo con `data/avatar`, `data/input/scripts`, `data/output`
y `data/results` como antes.

Los ejemplos anteriores de este documento que citan `data/input/scripts` o
`data/output/<script>` corresponden al runner **histórico**:
usa `uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py`
o `.\run.ps1 --legacy-layout ...` para reproducir exactamente esos comandos.
