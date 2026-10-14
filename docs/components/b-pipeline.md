# B1.1 → B1.2 → B2 → B3: flujo actual

El acceso de producción es `run.ps1`, que selecciona el estilo del
avatar y ejecuta B1.1 → B1.2 → B2. En estilo profesional añade B3
antes de generar las imágenes/vídeo; Casero termina la planificación visual
en B2. El estilo profesional continúa además con V1 → V2 → V3 → V4 y Gemini TTS;
Casero conserva su propia ruta de voz y CTA. Las etapas de voz
preservan los textos, identificadores y decisiones visuales de B2.
Consulta [voz profesional](professional-voice.md). Un TXT que contenga el JSON
`B2/merged_output.json` profesional puede iniciar directamente V1 sin ejecutar
B1.1/B1.2/B2/B2.1; ver [importación B2 profesional](professional-merged-input.md).

Los prompts activos viven en `src/ai_video_factory/bots/prompts/`.
Las referencias históricas B8 están disponibles en el historial Git,
no en el árbol actual.

El motor actual carga sus documentos de instrucciones desde
`src/ai_video_factory/bots/prompts/` on every run. Prompt edits take effect immediately;
the runner does not require checksum updates. A transport note for Responses API
Structured Outputs is appended at runtime.

- B1.1 runs once against the complete, unmodified script. The runtime resolves
  unique verbatim source anchors into contiguous blocks with no gaps, overlaps,
  duplication or content alteration.
- B1.2 receives the **entire original script and every materialized B1.1 block**
  in **one** Structured Outputs call. It directs the rhythm of the complete YouTube
  narration using the blocks' types and emotional transitions. Beats are narrative
  pulses, not mandatory cuts or isolated visual assets. Frozen block boundaries
  and exact source slices are preserved, with at most 40 whitespace-separated
  words and 120 stripped characters per beat (no minimum character limit).
- B1.2 first segments the complete video in one response. The application
  restores exact source punctuation and whitespace, then verifies that the
  canonical beat texts reconstruct every block and the entire original script
  **exactly**. If validation fails, Responses API retries identify the concrete
  errors across blocks and request only the affected B1.1 block groups; the
  application merges those groups and revalidates the complete plan, allowing up
  to two correction attempts per failing block. The authoritative request and
  result are stored in `B1.2/input.json` and `B1.2/output.json`, rejected
  attempts in `B1.2/rejected_attempt_<n>.json`. Once fully validated, the
  application persists each block's beats under `B1.2/block_<id>/output.json`
  for downstream checkpoints and `B1.2/merged_output.json` for full-video readers.
- Professional B2 directs consecutive batches of up to four B1.2 blocks in one
  Responses API conversation. The raw script is not sent; each batch includes
  frozen source beats, the narrative core, the original avatar reference, and
  any CTA-reserved beat groups. Each batch is validated and
  accepted before the next is requested. The application validates cumulative
  audio runs and visual grids across accepted batches; if validation finds
  specific failing beats, B2 returns only those beat groups and the application
  merges them while preserving all other decisions and earlier corrections.
  The final B2 plan covers every beat and keeps the 120-character maximum for
  consecutive `audio=true` narration across block and batch boundaries.
  Rejected responses are stored in `B2/rejected_attempt_<n>.json`. Validated
  `B2/block_<id>/output.json` and `B2/merged_output.json` continue to serve
  existing downstream components.
- **Casero B2 remains per-block and parallel.** It preserves upstream narration
  and assigns the existing four visual types under its own unchanged rules.
  Professional B2.1 remains a full-video audit of the frozen visual carriers.
- `--from B1.2` and `--from B2` still consume validated checkpoints, but
  incomplete per-block outputs are **not** stitched together into a supposedly
  global B1.2 or professional B2 decision. Replanning those stages requires
  a new whole-video call. Resuming professional B2 reuses complete CTA detector
  and director checkpoints; if any director output is missing, it reuses the
  detector groups and regenerates all CTA directors after B2.

## Professional B2: demonstrator identity, animation prompts and speaking intent

The professional bot keeps exactly one mandatory final presenter-return point:
the **last** beat of every `close` block reuses the original avatar image with
`visual_type: "avatar"`, `prompt: null`, `video_prompt: null`,
`reference: false` and `audio: true`. The first beat of `intro` is
**not** forced to use the avatar; its carrier is selected editorially. The
professional output schema enforces the closing anchor.

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

Professional `audio:true` is reserved for the supplied avatar narrator while
the reference narration setup and the narrator's physical placement/posture
class are preserved. Camera angle, framing and crop may vary, and small props
such as a phone, paper or product are allowed. Standing up from a seated setup,
lying down, walking elsewhere, changing seats/positions or moving to another
environment requires `audio:false` voice-over coverage. A narrator who remains
in the narration setup is never represented as a frozen still: generated
full-screen coverage uses `media_video + reference:true + audio:true`.

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
Professional B2 directs the video through one sequential Responses API
conversation, in batches of up to four blocks. A lettered logical grid uses a
stable uppercase label plus a one-based frame number at the start of each
prompt (for example, `A1. ...`, `A2. ...`). Beats carrying the same label may be
nonconsecutive and interleaved with other grid labels or ordinary/avatar beats;
the planner collects them by label, validates complete ordered numbering, and
allows up to twenty panels per sequence. Still-only sequences are packed into
physical grids of up to nine panels; sequences containing video use 2x2 grids
to preserve native 1080p panels. Lettered groups remain block-local and are
split at CTA boundaries. Existing numeric consecutive runs remain supported
for saved plans and legacy B2 output.

**Strict professional audio/type contract:** `media_image` MUST have
`audio: false` (ordinary master narration may still play as voice-over).
The current professional B2 contract reserves `audio: true` for the
unchanged original `avatar` and `avatar_media`. Both `media_image` and
`media_video` require `audio: false`; silent generated `media_video`
uses observational I2V. The professional Pydantic model
rejects invalid speaking stills, allowing the existing automatic B2
schema-correction retry to request a corrected result. Video planning
also rejects legacy saved plans containing `media_image + audio:true`
instead of accidentally treating those stills as speaking videos.
`avatar` and `avatar_media` continue to require `audio: true`.
The `casero` bot's output contract and grid prompt are unchanged.

The code-owned high-end phone-photography style remains separate from B2:
`effective_image_prompt()` injects it during generation, after any
numeric continuity marker.

## B3 profesional: continuidad de cámara en rachas habladas

B3 se ejecuta solo en estilo profesional, después de que B2 haya quedado
validado, y únicamente para rachas máximas de `audio:true` que tengan **más de
dos beats** y contengan **al menos un beat cuyo `visual_type` no sea
`avatar`**. Las rachas de uno o dos beats y las rachas compuestas enteramente
por `avatar` no entran en B3 y permanecen exactamente como B2 las dejó. Las
rachas elegibles pueden cruzar límites de bloque. B3 no cambia texto, IDs,
`audio`, `visual_type`, `prompt`, `video_prompt` ni `reference`; añade
solo una capa de planificación de toma.

Cada beat hablado recibe `continuous`. `continuous:true` significa que el
beat continúa exactamente la misma generación A2V/toma iniciada antes.
`continuous:false` abre una nueva toma. El primer beat de cada racha siempre
abre una toma, pero puede reutilizar el avatar base o una imagen B2 existente y
por tanto no necesita obligatoriamente una nueva imagen.

B3 puede cortar entre beats para cambiar de framing, ángulo o crop cuando ese
cambio mejora de forma útil la claridad, el énfasis o el ritmo. No puede usar el
corte para cambiar el setup de narración ni la posición/postura lógica del
avatar. La regla por defecto es **preservar B2**: `b3_shot_video_prompt` es
opcional y queda en null si el movimiento ya decidido por B2 sirve. Un primer
beat `avatar` normal puede arrancar la toma con `continuous:false`,
`b3_shot_prompt:null` y `b3_shot_video_prompt:null`. Un corte nuevo no
fijado por B2 lleva `b3_shot_prompt`; su start image se genera secuencialmente
usando la imagen efectiva de la toma anterior como referencia. Los beats
`continuous:true` no generan otra start image y se agrupan en el mismo trabajo
A2V.

Antes de llamar a B3, el runtime calcula el plan real de grids de B2, incluidos
los paneles filler. Esos paneles son inmutables: nunca se extraen del grid para
regenerarlos de forma aislada. Un panel hablado que actúa como start image fija
obliga a empezar una nueva toma y B3 adapta los beats libres alrededor de él.
Un panel `avatar_media` es solo apoyo visual y no fuerza por sí mismo otro
A2V. Si dos o más grids fijos dentro de una misma racha hablada no pueden
mantener continuidad entre sí, ambos se respetan tal como vienen; esa es la
única excepción permitida a la continuidad visual normal de B3.

El `visual_plan.json` conserva B2 intacto y añade la metadata B3
(`b3_group_id`, `continuous`, prompts de toma, referencia al beat anterior y,
cuando corresponde, metadata del grid). Las nuevas start images de B3 se
guardan aparte bajo `images/b3_shots/`; sus estados de petición pagada se
protegen y contabilizan igual que el resto de imágenes.

## Scripts, avatares y checkpoints del flujo administrado

El acceso público de producción es `run.ps1`. En la primera
ejecución, coloca el PNG del avatar y el guion TXT en `data/`;
el lanzador organiza automáticamente
`data/<avatar>/<guion>/<guion>.txt` y crea los metadatos persistentes del
avatar en `data/<avatar>/meta/`. Para los siguientes guiones de ese
avatar, deja cada TXT pendiente dentro de `data/<avatar>/`.
El estilo `casero` o `profesional` se fija por avatar y no cambia
silenciosamente.

```powershell
.\run.ps1 historia_roma monje --style profesional
.\run.ps1 historia_roma monje --from B2
.\run.ps1 historia_roma monje --images-only
.\run.ps1 historia_roma monje --sync-only
```

Los estados de trabajo y los manifiestos verificables residen en
`.video_factory_work/<avatar>/<guion>/`. Las salidas visibles
se publican en `data/<avatar>/<guion>/outputs/` y `media/`.
B1.2 y B2 Profesional guardan además sus solicitudes y respuestas
**globales** en la raíz de cada etapa, conservando `output.json` por bloque
para los consumidores de checkpoints. `visual_plan.json` conserva la
identidad y el orden de los beats.
Los checks SHA-256 impiden reanudar con otro guion o reutilizar
resultados de pago de una solicitud distinta. `--rerun` archiva
expresamente la ejecución anterior antes de empezar una nueva.

El módulo `scripts/pipeline/run_b_pipeline.py` es el motor del
lanzador, no un segundo layout de producción.

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
finishes: B1.1 after its validated output, B1.2 after the single complete
whole-video result and its materialized block checkpoints, then B2 after one
whole-video professional result or the parallel Casero block results. Once the visual plan and final report are saved, it prints the run
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
`B1.1/`, `B1.2/block_<id>/`, `B2/block_<id>/` and `B3/group_<id>/` directories, with
`B1.2/merged_output.json`, `B2/merged_output.json`, `B3/merged_output.json`, the application-owned
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
whitespace and punctuation, a 40-word and 120-character maximum per B1.2
beat (with no character minimum), type/ID preservation, and mandatory intro/close avatars. Whether a media
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
El lanzador selecciona el avatar y el guion desde el layout
administrado `data/<avatar>/`. Sin argumentos, muestra los menús
interactivos. Para iterar únicamente los bots, sin nuevas llamadas
de imagen, ejecuta:

```powershell
.\run.ps1 historia_roma monje --no-image
```

El runner termina después de escribir B2 y `visual_plan.json`, sin llamar
a la API oficial de OpenAI para imágenes. Cuando quieras
generarlas, ejecuta `--images-only` a partir del B2 guardado. Cuando los PNG,
Fish/STT y Postgres/R2 están listos, el runner encola automáticamente los beats
que requieren LTX. El controlador de capacidad de Salad sigue siendo un servicio
independiente: el runner no despliega GPUs.


## CTA ebook en Profesional: detector + director + merge protegido

Tras B1.2, el **merged B1.2 completo** alimenta a CTA Detector. Cuando hay
portada de ebook, B2 Profesional espera a disponer de los IDs de grupos CTA
reservados para planificar la progresión visual global de entrada y salida. El detector devuelve exclusivamente grupos
cronológicos de IDs de beats completos que promocionan el ebook; nunca cambia
texto ni segmenta beats. El bot **B2 CTA Profesional** recibe cada grupo, la
imagen original del avatar y la portada persistente del ebook, y asigna con
libertad creativa `visual_type`, `prompt`, `video_prompt`, `audio`,
`reference` y `reference_sources` (presenter, ebook o ambas referencias).
El campo `reference` sigue siendo booleano para compatibilidad con B2.1 y B3.
La selección real se guarda separada como `cta_reference_sources`.

La asignación B2 original se preserva en sus checkpoints; el **merged B2**
y `visual_plan.json` sustituyen únicamente las asignaciones visuales de los
beats detectados. Las salidas CTA son inmutables para B2.1: todo parche de
B2.1 que toque esos IDs se descarta; también se excluyen de sus split
candidates. El merge revalida límites consecutivos de `audio=true` y grids
antes de continuar. Los grupos A2V de B3 y los trabajos LTX cortan
forzosamente al entrar y salir de un CTA, sin compartir tomas con la narración
normal. B2.2 sigue refinando el movimiento post-B3 contra los PNG definitivos.

La biblioteca del avatar fija la portada original con
`ensure_ebook_covers` (selección inicial mediante `--ebook-cover` y posterior
reutilización). **Solo se activa el circuito CTA del ebook cuando hay una portada
configurada**; las ejecuciones Profesional existentes sin ebook siguen el
pipeline habitual sin requerir nuevas entradas. Cuando está configurado, cada
run Profesional guarda la copia verificable `ebook/cover_1.png`. El generador OpenAI Images recibe realmente los PNG
correspondientes a cada `cta_reference_sources` y los vincula al fingerprint
de la solicitud. Ninguna referencia se sustituye silenciosamente.

**QR:** no es parte de ningún prompt de imagen ni LTX. El compositor inserta
`qr.png` auténtico y validado por SHA-256 en la esquina superior derecha
durante cada grupo CTA. Desde el inicio del primer CTA hasta 90 segundos
después de su final permanece visible, incluso sobre beats narrativos
posteriores. **Además, `zellenne.com` aparece al pronunciar por primera vez
«QR» dentro de cada CTA** y desaparece al acabar ese CTA, sin heredar los
90 segundos adicionales del QR. El comienzo se deriva de la palabra ya
alineada con OpenAI STT (campo `qr_spoken_seconds` de la timeline); si ese
CTA no contiene un «QR» pronunciado, no aparece la web. El texto se genera
por código, blanco con contorno negro fino (2 px), centrado horizontalmente,
a 145 px del margen inferior para situarse por encima de subtítulos habituales.
No se incorpora a los prompts ni a los fotogramas LTX. Los intervalos usan la
timeline STT exacta; no se vuelve a grabar la narración original. Ningún beat
posterior necesita instrucciones sobre el QR. El vídeo con overlay se verifica
antes de declararse completado.

Artefactos:
- `CTA Detector/output.json` — únicamente grupos de beat IDs.
- `B2 CTA/group_N/output.json` — decisiones creativas del CTA.
- `visual_plan.json` — `ebook_cta_groups`, `ebook_reference`,
  `cta_group_id` y `cta_reference_sources`.
- `video/runs/<fingerprint>/with_ebook_qr.mp4` — composición final.

## B2.2 profesional: adaptación del movimiento después de B3

La generación LTX profesional aplica **B2.2** tras B2.1, B3, los PNG finales
(incluido el acabado/recorte de grids y las nuevas tomas B3) y el alineamiento
STT, pero antes de enviar trabajos pagados a Postgres/Salad. B2.2 es un adaptador
multimodal: ve los fotogramas iniciales **reales** de los trabajos, las
instrucciones de movimiento efectivas post-B3 y el desarrollo cronológico del
guion, incluidos los beats estáticos intermedios. Retorna estrictamente pares
`beat_id` y `video_prompt` por trabajo elegible; no cambia `visual_plan.json`.

Solo se adapta I2V `media_video + audio:false` y tomas A2V cuyo primer beat
no sea `avatar` y ya tengan un movimiento efectivo. Un A2V que agrupa beats
produce una sola instrucción, identificada por el primer beat. El código mantiene
intactos el prefijo canónico de habla, el negativo fijo A2V, el audio y los
cortes/cámara de B3. Si la imagen contradice la intención de B2, el bot
reestructura la acción para que pueda animarse en lugar de bloquearla.

B2.2 manda los vídeos elegibles en grupos cronológicos multimodales acotados,
con el texto completo de los beats posteriores y fotogramas próximos para
evitar anticipar movimientos o repetir acciones. Los resultados se validan
por conjunto exacto y orden de identificadores. La respuesta validada se
cachea por hashes de imágenes, prompts, contexto, instrucciones y modelo en
`video/b22/groups/`; la identidad de trabajos LTX se vuelve a calcular con
los nuevos prompts. Un fallo de validación no encola trabajos pagados.

Bot: `src/ai_video_factory/bots/prompts/b2_2_professional.md`.
Código: `src/ai_video_factory/workflows/b22_motion.py`.

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
Para detener la ejecución antes de vídeo y reanudarlo
posteriormente se utiliza el mismo lanzador administrado:

```powershell
.\run.ps1 test2 Jorge --no-video
.\run.ps1 test2 Jorge --generate-video
```

`--images-only` y `--resume-audio` también activan LTX cuando la otra
mitad de artefactos está disponible. Las comprobaciones automáticas son
técnicas y no acreditan visualmente la sincronización de labios o la
inmovilidad de cámara; se requiere revisión de una generación real en Salad.

## Organización de guiones por avatar

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
sobrescribirlo. El único acceso directo admitido por `run.ps1` es el directorio
administrado `data/<avatar>/<guion>/`. Para probar el motor directamente
sin el lanzador, `scripts/pipeline/run_b_pipeline.py` conserva su interfaz
técnica de checkpoints y rutas explícitas; no constituye otro flujo público
ni ofrece el antiguo modo `--legacy-layout`.
