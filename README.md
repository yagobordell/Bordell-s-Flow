# AI Video Factory

## Professional voice: V1 → V2 → V3 → Gemini 3.8

The `profesional` style now runs its voice directors **after validated B2**:
V1 sets the film's rhythm, V2 distributes each block's intent across its
beats, and V3 creates exact-text speech fragments with sustained
`speech_metadata.style` and optional allowlisted human vocal events
(`<breath>`, `<sigh>`, `<short pause>`, etc.). Gemini 3.8 is its only TTS
provider; it has an independent saved voice for each professional avatar.
`casero` retains Fish, MiniMax and ElevenLabs.

First professional run:
```powershell
.\run.ps1 ejemplo Jorge --style profesional --voice-id Algenib
```
Set `GEMINI_API_KEY` and `OPENAI_API_KEY`. Use `--no-audio` or `--stop B2`
to skip post-B2 professional audio, or `--from B2` to re-run from a saved
B2 checkpoint. `--resume-audio` reuses safe verified paid blocks; unknown
paid POST outcomes require reconciliation, not blind retries.
See [professional V pipeline and recovery](docs/components/professional-voice.md).
The historical sections below include legacy Casero/Fish examples.


## Nuevo flujo de archivos: un TXT suelto es un guion pendiente

`run.ps1` usa ahora la estructura **`data/<avatar>/`**. Para el primer
uso, coloca `Jorge.png` y `test01.txt` directamente en `data/` y
ejecuta `.\\run.ps1 test01 Jorge`. El programa mueve automáticamente
el PNG a `data/Jorge/Jorge.png` y el TXT a
`data/Jorge/test01/test01.txt`. Las carpetas del guion contienen
`outputs/B1.1`, `outputs/B1.2`, `outputs/B2`,
`media/images`, `media/audio`, `media/preview` (vídeo de solo
imágenes) y, al terminar LTX, `test01.mp4` en la raíz de su carpeta.
Cada etapa se copia cuando está terminada y se comprueba con SHA-256,
sin esperar a que concluya el vídeo completo. Si hay archivos generados
solo en el espacio interno, utiliza
`.\\run.ps1 test01 Jorge --sync-only` para publicarlos sin nuevas llamadas
de generación. Durante la primera ejecución se crea
`data/Jorge/meta/`: ahí se guardan el QR (`qr.png` y `qr.json`),
la configuración de voz (`voice.json`), el audio original de clonación
si lo hay y **`style.json`**. El estilo B2 se elige una sola vez por avatar;
los siguientes guiones reutilizan el estilo guardado sin volver a preguntar.
Un `--style` incompatible se rechaza para evitar cambios accidentales.

Después, guarda cada nuevo TXT **suelto en `data/Jorge/`**. Los TXT sueltos
indican trabajo pendiente; las carpetas indican ejecuciones iniciadas y el
archivo `manifest.json` confirma que el vídeo final se ha publicado.
Los checkpoints internos permanecen fuera de esa estructura,
en `.video_factory_work/<avatar>/<guion>/`, para permitir reanudación.

```powershell
.\\run.ps1 test01 Jorge
.\\run.ps1 test02 Jorge --no-video
.\\run.ps1 test02 Jorge --generate-video
# Repetir test02 desde B1.1, conservando la ejecución anterior:
.\\run.ps1 test02 Jorge --rerun
```

Los apartados antiguos del README que muestran `data/input`,
`data/avatar` o `data/output` corresponden al **runner heredado**.
Puedes seguir esos ejemplos con `.\\run.ps1 --legacy-layout ...`.
El flujo nuevo está detallado en
[la documentación de B-pipeline](docs/components/b-pipeline.md).


Pipeline modular para generar vídeos cinematográficos 16:9 a partir de un guion terminado. La
orquestación combina planificación estructurada, generación de audio e imagen, vídeo en GPU remota,
upscale y composición final reproducible.

## Estado actual

El único flujo de **bots de planificación** es B1.1 → B1.2 → B2. Procesa el
guion sin modificarlo, genera beats por bloque y selecciona estrategias visuales
sin convertirlas a los contratos anteriores de escenas/shots.

El anterior runner de producción de extremo a extremo y los bots que lo alimentaban
se han retirado. **Todavía no existe un recorrido automatizado B2 → vídeo final**:
las fases de narración, Qwen, LTX, Real-ESRGAN y Remotion conservan herramientas
y clientes independientes, pero requieren la migración explícita a los nuevos
beat IDs y tipos visuales antes de poder componerse en una única ejecución.
Consulta [estado de la integración](docs/operations/production-runner.md).

Los servicios GPU de Salad, la cola autoritativa en Postgres, R2, el Capacity
Controller y los mecanismos de reintento/recuperación permanecen disponibles.
Ideogram sigue siendo opcional; Qwen es el generador de imágenes utilizado
en las herramientas de imagen.

## Requisitos

- Python 3.12+
- uv 0.12.18
- Windows PowerShell para los scripts operativos de Salad y los runners GPU controlados
- Node.js 22+ y npm
- FFmpeg + ffprobe
- Credenciales para OpenAI, SaladCloud, Cloudflare R2 y Postgres/Supabase
- Hugging Face cuando lo requieran los modelos configurados
- Docker solo para construir, preparar o probar workers; no para una ejecución normal con Salad ya preparado

## Instalación

```powershell
python -m pip install "uv==0.12.18"
uv sync --locked --extra dev
.\.venv\Scripts\Activate.ps1

Copy-Item .env.example .env

Push-Location .\remotion
npm.cmd ci
Pop-Location
```

Completa `.env` con las credenciales y configuración necesarias. Para usar SaladCloud,
configura `SALAD_ORGANIZATION`, `SALAD_PROJECT` y `SALAD_API_KEY` con los datos
de **tu propia cuenta**; el manifiesto `deploy/salad/services.json` no selecciona ninguna cuenta.
Para una infraestructura de Salad independiente, usa también una base de datos Postgres
separada: compartir la cola GPU entre cuentas no aísla las generaciones ni los costes.
Los secretos y los artefactos generados no deben versionarse. Python se instala desde `uv.lock`;
cualquier cambio de dependencias
debe actualizar `pyproject.toml` y `uv.lock` conjuntamente.

## Ejecución de B1.1 → B1.2 → B2

### Comando corto en PowerShell

Desde la raíz del repositorio puedes usar `run.ps1` sin escribir cada vez
`uv run --locked --extra dev python ...`. Este acceso directo utiliza
`data/input/scripts/` como biblioteca de guiones y `data/avatar/` para
los avatares. El comando Python completo mantiene su ubicación predeterminada
anterior (`data/input/`) para no romper automatizaciones existentes.

```powershell
.\run.ps1 test2 Jorge --no-image   # Bots B + Fish TTS, sin imágenes
.\run.ps1 test2 Jorge --no-audio   # Bots B + imágenes, sin Fish TTS
.\run.ps1 test2 Jorge --no-image --no-audio  # Solo B1.1, B1.2 y B2
.\run.ps1 test2 Jorge              # Bots B + TTS + imágenes
.\run.ps1 test2 --regenerate-audio # Solo Fish director + audio nuevo, sin avatar
.\run.ps1 test2 --resume-audio     # Recupera una tarea Fish TTS guardada
.\run.ps1 test2 --images-only      # Genera o recupera imágenes del B2 guardado
.\run.ps1                         # Menú interactivo de guiones y avatares
```

Cada ejecución completa desde B1.1 sustituye `data/output/<guion>` y vuelve a
generar el run desde cero, aunque esa carpeta ya exista. El borrado local no
cancela trabajos remotos ya enviados; podrían terminar y generar cargos aunque
sus estados locales se hayan eliminado. Usa `--images-only` o `--resume-audio`
si quieres recuperar una tarea anterior.

Fish Audio utiliza el documento de instrucciones
`src/ai_video_factory/bots/prompts/fish_audio.md`, copiado del BotFish
adjunto. Empieza **al finalizar B1.1**, en paralelo con B1.2, recibe el mismo guion sin modificar
y solo inserta etiquetas entre corchetes. Su salida se envía como `text`
al endpoint TTS de OpenSpeaker con la voz asignada al avatar; si se elige
la voz predeterminada se usa `AI33_FISH_VOICE_ID`. La generación guarda
`audio/runs/<id>/input.json`, `output.json`, `task.json` y el archivo
de audio, además de `audio/latest.json`. Configura `AI33_API_KEY` junto
con `OPENAI_API_KEY` en `.env`; no se ejecutan tareas de Salad para esto.
`--no-audio` impide arrancar el director y evita toda solicitud TTS,
mientras que `--regenerate-audio` crea una **nueva** generación con los
bots B y la etapa de imágenes totalmente deshabilitados. Si una tarea
remota se interrumpe, `--resume-audio` retoma el `task_id` anterior,
sin repetir una solicitud de pago ni volver a ejecutar el director.
El audio de Fish/AI33 no ha sido validado aún mediante una muestra real:
la etiqueta de origen de voz de OpenSpeaker no garantiza que su puente
de síntesis ejecute el modelo Fish S2.

Las extensiones `.txt` y `.png` son opcionales. Las opciones adicionales
(`--from B2`, `--output`, etc.) se pasan al runner existente. No se
cambian el modelo, los prompts, los costes, los estados ni el fallback de
imágenes. Si PowerShell bloquea la ejecución de scripts por su política
local, puedes seguir usando el comando Python completo.

Guarda los guiones UTF-8 directamente en `data/input/` (por ejemplo,
`historia_roma.txt`) y los PNG nuevos en `data/avatar/` (por ejemplo,
`Jorge.png`). Al usar el avatar, el runner lo organiza automáticamente como
`data/avatar/Jorge/Jorge.png`, con `qr.png`, `qr.json` y `voice.json` **en la
misma carpeta**, sin subcarpetas para QR ni voz. `--list-avatars` y `--avatar Jorge`
aceptan tanto los PNG nuevos de la raíz como los avatares ya organizados.
Los archivos de esta biblioteca y los guiones son locales y están ignorados por Git.

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --list-scripts
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --list-avatars
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt --avatar monje.png
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt --avatar monje.png --from B1.2
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt --avatar monje.png --from B2
# Save only B1.1 (no Fish Audio, B1.2, B2 or images):
.\run.ps1 historia_roma monje --stop B1.1
# Run only B1.2 from the saved B1.1 checkpoint:
.\run.ps1 historia_roma monje --from B1.2 --stop B1.2
# Run only B2 from saved B1.1/B1.2 outputs, without images:
.\run.ps1 historia_roma monje --from B2 --stop B2
# Finish after B2, without generating images or invoking AI33/OpenAI image APIs:
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt --avatar monje.png --no-image
# Resume standard official OpenAI image requests without repeating any planning bot:
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt --images-only
```

En una terminal interactiva, sin argumentos, el runner ofrece primero el
menú de guiones y después el de avatares. Si eliges el guion con `--script`,
puedes elegir el avatar en el segundo menú; para automatizarlo especifica
ambos argumentos. El runner comprueba que el avatar es un PNG válido antes
de generar y copia sus bytes a `data/output/<nombre-del-guion>/avatar.png`.

La primera vez que se selecciona un avatar sin QR, la CLI pide el enlace del
ebook y **Segno lo codifica localmente** como QR PNG, sin llamadas a GPT ni a
ninguna API. El QR y la URL se guardan en
`data/avatar/<nombre-avatar>/qr.png` y `qr.json`, de modo que las siguientes
generaciones con el mismo avatar los reutilizan. También se guarda una copia
`qr.png` en la carpeta de salida del guion y se referencia desde
`run_report.json` y `visual_plan.json`. El QR todavía no se muestra sobre
el vídeo. Si había un QR generado por GPT, se sustituye automáticamente
usando la URL guardada, sin pedirla otra vez.

Para una primera asignación no interactiva usa
`--qr-url https://ejemplo.com/ebook`. Para crear o reparar solo un QR sin
guion, créditos, ni clave de OpenAI:

```powershell
.\run.ps1 --qr-only --avatar Jorge --qr-url "https://ejemplo.com/ebook"
```

`--no-image` omite las imágenes B2, pero el QR local se prepara igualmente.
Los tests de CI comprueban con ZBar que el PNG se decodifica a la URL exacta.
Antes de publicar, escanea el QR con tu móvil y comprueba la página de venta.

### Voz persistente por avatar

Al seleccionar por primera vez un avatar, además del QR, la CLI pide un
**Voice ID de AI33Pro**, el **nombre de un audio de referencia** situado en
la misma carpeta que el PNG o `q` para guardar la voz predeterminada.
La elección se conserva en `data/avatar/<nombre-avatar>/voice.json` y no se
vuelve a preguntar. Solo utiliza grabaciones propias o con autorización expresa.
En una ejecución no interactiva sin selección previa se conserva la voz por
defecto, para no interrumpir automatizaciones existentes.

```powershell
# En el primer run, Jorge.wav puede estar junto a data/avatar/Jorge.png;
# después ambos quedan en data/avatar/Jorge/. También puedes crear esa carpeta
# de antemano y poner Jorge.png y Jorge.wav dentro.
.\run.ps1 test2 Jorge --voice-audio Jorge.wav
.\run.ps1 test2 Jorge --voice-id fishaudio_mi_voz
.\run.ps1 test2 Jorge --default-voice
```

Las opciones anteriores sirven para la **primera** asignación y no cambian una
voz ya guardada. El audio de referencia tiene límite de 10 MiB; OpenSpeaker
recomienda 3–30 segundos de voz clara. La clonación se solicita cuando la
generación necesita TTS, no durante `--no-audio` ni `--stop`. El Voice ID
resultante se guarda para futuras generaciones y el run copia su selección a
`avatar_voice.json` para que `--regenerate-audio` y `--resume-audio` usen la misma voz.
Si una solicitud de clonación queda con resultado desconocido, no se repite
automáticamente: consulta la cuenta de AI33Pro y, si recuperas el Voice ID,
introduce `--voice-id ID` con ese avatar para resolver la asignación pendiente.
Los nombres de los campos del formulario de clonación deben contrastarse con
la documentación autenticada de AI33Pro; no se ha hecho una prueba real con créditos.

Los avatares ya usados con la estructura antigua se migran al seleccionarlos:
`data/avatar/.qr/<avatar.png>/` y `data/avatar/.voice/<avatar.png>/` se integran
con el PNG en su propia carpeta. Se conservan la URL, el Voice ID y cualquier
clonación pendiente; el runner no vuelve a pedir estos datos. El audio de
referencia asignado se mueve junto al PNG. Si encuentra un archivo de destino
con el mismo nombre, detiene la migración sin sobrescribirlo. Los runs previos
conservan sus copias de avatar y voz.

Cada guion escribe en `data/output/<nombre-del-guion>/`: input/output de
B1.1, output por bloque y merge de B1.2/B2, `visual_plan.json` y
`run_report.json` con costes, tiempos y metadatos. El coste estimado de GPT-6 Luna
incluye los bots B y las llamadas del director Fish (también al regenerar audio),
sin duplicarlas al reanudar. No incluye los cargos independientes de TTS, STT ni
imágenes, y no sustituye a la facturación real de la API. Ambos JSON incluyen el
avatar elegido, su imagen local de la ejecución, dimensiones y SHA-256; el
modelo B2 sigue recibiendo y devolviendo sus contratos originales. Para repetir
solo etapas posteriores, usa `--from B1.2` o `--from B2`; el runner valida y
reutiliza los artefactos previos del mismo guion. `--stop` es inclusivo y puede
combinarse con `--from` siempre que la etapa final no preceda a la inicial.
Al usar `--stop` no se inicia Fish Audio ni se generan imágenes, y el informe
registra `stopped_after`; las ejecuciones sin `--stop` mantienen su flujo normal.
La consola
imprime una línea de tiempo y coste inmediatamente después de terminar cada bot.

### Costes observados de Salad (LTX)

Durante la generación de vídeo LTX, el runner consulta las instancias de los
grupos ltx25 (A2V) y ltx25_i2v (I2V) cada 15 segundos. Suma los segundos
en estado running por réplica; la descarga y el arranque no generan coste.
Muestra un subtotal provisional cada minuto y otro al terminar la etapa.
El diario persistente video/salad_costs.json permite recuperar importes
observados sin duplicar periodos al reanudar el vídeo.

run_report.json incluye salad_costs y
api_costs.priced_subtotal_including_salad_usd. El campo
api_costs.estimated_total_usd se mantiene exclusivamente para OpenAI;
estimated_total_including_salad_usd solo existe como importe cuando los
dos componentes están completos.

Para las RTX 5090 High de ambos servicios se usa el precio público de
Salad de 0,50 USD por GPU/hora, vigente desde el 12 de septiembre de 2026.
Los precios personalizados pueden indicarse en .env mediante
SALAD_LTX25_USD_PER_GPU_HOUR y SALAD_LTX25_I2V_USD_PER_GPU_HOUR.
Si cambian la GPU o la prioridad y no hay precio configurado, no se
presupone una tarifa. Fuente: https://salad.com/pricing.

El cálculo refleja el coste observado de los grupos completos **durante
la fase de vídeo**, incluidas las réplicas encendidas sin procesar trabajos.
No es un coste exacto por beat ni una factura: los grupos pueden procesar
trabajos ajenos y continuar generando cargos antes o después del vídeo,
por ejemplo, durante el drenaje del Capacity Controller. Las interrupciones
de la telemetría y los periodos no observados se marcan como parciales,
nunca como coste cero. Consulta la facturación de Salad para conocer el
importe real de toda la cuenta.


B1.1, B1.2, B2 y el director de voz Fish usan el mismo modelo de
texto `OPENAI_MODEL=gpt-6-luna` y `OPENAI_REASONING_EFFORT=medium`.
No existe una configuración independiente `OPENAI_B_MODEL`.

Al terminar B2, el runner genera imágenes PNG mediante la API oficial de OpenAI,
sin AI33 para imágenes. Los beats con `description` no vacía y sin numeración
de continuidad conservan una imagen por beat. Las secuencias consecutivas
numeradas de B2 (`1.`, `2.`, etc., hasta nueve) se agrupan en **una única
generación de grid** por secuencia; también participan los beats `media_video`,
cuya imagen será la futura start image. Cada imagen individual o grid usa
**una única solicitud estándar** (`n=1`).
Las descripciones que contienen la palabra completa `avatar` (sin distinguir
mayúsculas) usan `/v1/images/edits`: adjuntan la copia `avatar.png` del run
como referencia de identidad, previa comprobación de su SHA-256 en el plan.
Las demás usan `/v1/images/generations` sin imagen de entrada. La descripción
original de B2 permanece inalterada y solo se añade una instrucción de referencia
al prompt enviado para esas ediciones. Si falta el avatar o ha cambiado, se
detiene la solicitud antes de generar imágenes de pago. Se mantiene el prefijo
`iphone 6 photo done by an elderly:  `, Flare por defecto, `low`, PNG
y `1280x720` para imágenes individuales. Configura `OPENAI_API_KEY`
en `.env`; AI33 sigue utilizándose únicamente para la voz. Los beats
`avatar` sin descripción no crean imágenes. Un máximo de cuatro generaciones
se procesan a la vez.

Los grids son siempre 2×2 (2–4 beats), 3×2 (5–6) o 3×3 (7–9). Los
huecos se completan primero con imágenes siguientes no reservadas y,
si no bastan, con las anteriores disponibles. Cada beat pertenece a
una sola generación: las imágenes de relleno no se generan aparte.
Si no hay suficientes imágenes distintas, la validación falla antes de
crear solicitudes de pago. Cada panel ocupa 1280×720 píxeles en el grid:
los PNG completos miden 2560×1440, 3840×1440 o 3840×2160.
Los grids se generan con calidad `medium` en OpenAI (Flare o Sunburst,
B2 casero o profesional); las imágenes individuales conservan `low`.
La calidad efectiva de cada solicitud queda registrada en su estado y
fingerprint para evitar repetir o mezclar generaciones de pago.
Los tamaños superiores a 2560×1440 son experimentales según OpenAI;
se debe revisar visualmente la distribución real de paneles. El prompt
de cada grid se compone **por código** con la cabecera
`iPhone 6 photograph done by an elderly:`, la línea `2x2 grid 16:9`
(o `3x2`/`3x3`), las descripciones numeradas, una sección
`Voice over context:` con el `text` de cada beat incluido y la instrucción
`Do not add text to the image.`
Las descripciones numeradas por B2 mantienen literalmente su número;
las imágenes de relleno reciben por código el número de su posición
en el grid. Las filas y columnas se conservan en el mapa interno de
paneles para el recorte, sin añadirse al prompt. Si algún panel
menciona `avatar`, se adjunta el avatar verificado del run a la
solicitud completa del grid.

Tras guardar el PNG grande, un proceso **local, sin llamadas a la API**
detecta los separadores blancos cerca de su posición nominal. Calcula
sus bordes reales, deja un pequeño margen de seguridad dentro de cada
celda para evitar restos blancos y convierte cada panel a 1280×720.
Los PNG individuales se guardan por beat en `images/block_<n>/`
y se registran en `visual_plan.json:image_assets` y
`images/manifest.json:panel_assets`; los grids originales no se borran
y conservan el mapa de paneles en `visual_plan.json:image_grids`.
Los beats `media_video` reciben así su imagen inicial individual.
La vista previa puede usar estos recortes. Durante el montaje, cada
`media_image` recibe un zoom in/out suave centrado o un Ken Burns in/out
anclado en alguna de las cuatro esquinas, con movimiento durante el beat.
Se alternan los estilos por orden de imágenes y se conservan los PNG
originales; los beats `media_video`, `avatar` y `avatar_media` no
reciben ese movimiento. Si un separador no se puede localizar con
suficiente seguridad, el postproceso se detiene sin repetir ninguna
generación de pago: el grid original queda disponible para inspección
y `--images-only` puede reanudar los artefactos guardados.

Cada generación estándar guarda su estado antes del POST y conserva el
fingerprint del prompt efectivo para `--images-only`. Si la respuesta de pago
se pierde, el estado queda como `direct_submitting_unknown` y la petición no se
repite automáticamente. `--images-only` protege los estados anteriores de AI33
o Batch y exige reconciliarlos antes de lanzar solicitudes oficiales nuevas.
Una ejecución completa desde B1.1 sí elimina la carpeta anterior del guion.
`--no-image` (alias `--skip-images`)
permite guardar B2 sin hacer ninguna llamada a la API de imágenes.

**Límite actual:** esto genera un plan visual e imágenes fijas, **no** un vídeo final.
Los wrappers GPU independientes y los servicios de Salad siguen en el
repositorio; no están conectados automáticamente a este plan.
Consulta [la guía del pipeline B](docs/components/b-pipeline.md).

### Historial: recuperación A2V v3 (ejecutada, terminó en CUDA OOM)

El perfil de producción **v3** conserva 1280×720, 24 fps, los prompts,
el audio, la semilla y las dos etapas. Usa FP8_CAST y disk offload y deja
que el planificador DiffVAE calcule el tamaño de los tiles con la VRAM libre
real después de limpiar las cachés; retiene un margen adicional de 1 GiB
sobre las reservas internas del planificador. El antiguo límite fijo de 2 GiB
era inferior a sus gastos mínimos de modelo, reserva y características de
etapa 4: en `test02` produjo `usable_bytes=0`. El perfil v2 se conserva
solo para la identidad de los trabajos antiguos; **no se reenvía** como
solución. No hay todavía una prueba GPU de pago que demuestre que v3 completa
ese vídeo. Los errores de tile imposible son terminales al primer intento;
los OOM CUDA siguen limitados a dos intentos A2V.

Para recuperar el run guardado **sin regenerar B1, Fish TTS ni imágenes**:

1. Comprueba que los A2V previos están en `failed` con errores de CUDA OOM
   o presupuesto DiffVAE y que no quedan trabajos activos. Espera a que
   `ltx25` esté `stopped` con `pending=False`; entonces detén el único
   Capacity Controller para liberar el advisory lock de Postgres.
2. Construye y publica una **nueva etiqueta Docker** y prepara solo `ltx25`
   con `manage_salad_worker.ps1 -Action Prepare -Service ltx25 -Image <imagen-nueva>`.
   Nunca reutilices la imagen anterior: un `git pull` no actualiza Salad.
   Deja `ltx25_i2v` sin cambios.
3. Consulta `-Action Status -Service ltx25` y copia la referencia
   `repositorio@sha256:digest` de la nueva imagen a
   `SALAD_LTX25_AUTOTILING_A2V_IMAGE` en `.env`. La variable anterior
   `SALAD_LTX25_SAFE_A2V_IMAGE` identifica v2 y **no** habilita v3.
   La comprobación del runner falla antes de enviar un trabajo GPU si falta
   el nuevo digest o no coincide con el desplegado.
4. Arranca el Capacity Controller y verifica su salud. Recupera el run con
   `.\\run.ps1 test02 --generate-video`. El perfil v3 genera un fingerprint
   y un job ID nuevos; el reconciliador conserva ambos historiales antiguos
   y solo los supersede si Postgres confirma estados terminales recuperables.
   Nunca reinicies el run completo con `.\\run.ps1 test02 mecanico`.

### Nuevo intento instrumentado A2V v4 (sin repetir bots, TTS ni imágenes)

El intento v3 de `test02` terminó con CUDA OOM en dos intentos y los logs
de Salad no contenían su traceback completo. V4 **no cambia el modelo,
resolución, prompt, duración, audio, samplers ni presupuesto CUDA de v3**;
solo registra diagnósticos persistentes y utiliza un nuevo fingerprint y un
job ID para no reanudar el v3 terminal. El intento v4 se ejecutó y el último
A2V (7C) falló dos veces al reservar memoria fijada de CPU para Gemma.

En cada intento A2V el worker guarda JSON independientes en R2 bajo
`diagnostics/ltx-a2v/<job_id>/attempt-XX/`. Registra fases de Gemma,
audio, stage 1/2, tiling, DiffVAE y codificación, junto con VRAM libre,
reservada, pico y contadores OOM, memoria RSS/pinned/cgroup de Linux,
UUID/PCI de GPU si los facilita nvidia-smi, instancia y hora UTC.
Si Python captura la excepción, el último JSON contiene la cadena completa
del traceback y Postgres `gpu.jobs.last_error` incluye la última fase y la
clave de R2. Los checkpoints anteriores sobreviven incluso si el contenedor
muere sin ejecutar el manejador de excepciones. La escritura a R2 es
best-effort: un fallo de red o una muerte antes del primer checkpoint sigue
siendo un límite observable, no una garantía absoluta de diagnóstico.

Antes de pagar otro intento, actualiza `main`, confirma que Postgres no tiene
trabajos GPU activos, que `ltx25` está detenido y estable, y detén el único
Capacity Controller. Construye una etiqueta Docker nueva y prepara
**solo** `ltx25` mediante
`manage_salad_worker.ps1 -Action Prepare -Service ltx25 -Image <imagen-v4>`.
Copia el digest real mostrado por `-Action Status` a
`SALAD_LTX25_DIAGNOSTIC_A2V_IMAGE=<repositorio>@sha256:<digest>` en `.env`.
Las variables antiguas `SALAD_LTX25_SAFE_A2V_IMAGE` (v2) y
`SALAD_LTX25_AUTOTILING_A2V_IMAGE` (v3) **no habilitan v4**.
Reinicia y comprueba el controller y recupera el run existente con
`.\\run.ps1 test02 --generate-video`, nunca con el comando del run completo.

Después, tanto si hay éxito como si hay error, recupera todos los registros
por ID, sin el filtro de logs de Salad:

```powershell
uv run --locked --extra dev python scripts/salad/inspect_ltx_a2v_diagnostics.py --job-id <ID_DEL_NUEVO_TRABAJO>
```

El comando consulta Postgres y descarga todos los checkpoints y el traceback
a `data/output/ltx-a2v-diagnostics/<ID_DEL_NUEVO_TRABAJO>/`.
La recuperación conserva los manifiestos anteriores y no borra ningún coste.

### Recuperación v5: corregir Gemma y conservar los vídeos generados

Los dos `011-failed.json` del clip 7C confirmaron la misma RTX 5090, más
de 30 GiB de VRAM libre y un OOM en
`ltx_core.block_streaming.utils.alloc_buffer(..., pin_memory=True)`, durante
la preparación de Gemma. No ocurrió en difusión ni DiffVAE y **no se corrige
dividiendo el beat**.

La nueva imagen v5 aplica durante el build un parche **estricto al commit
de LTX fijado en Docker**. Si `cudaHostRegister` rechaza el registro y el
asignador de PyTorch también devuelve OOM al reservar un buffer de RAM fijada,
el cargador utiliza un buffer normal de CPU. Las transferencias H2D desde
estos buffers se fuerzan a ser bloqueantes; los buffers fijados conservan la
transferencia asíncrona. Se registra en R2 el tamaño solicitado, el código
de rechazo de `cudaHostRegister` y el fallback, y se capturan los límites
de memoria bloqueable (`memlock`) y cgroups Linux cuando están expuestos.
Puede ser más lento al utilizar el fallback y **no garantiza el éxito** si
también se agota la RAM real. No modifica prompts, vídeo, audio ni el límite
preexistente de 12 segundos por grupo de avatar.

Para recuperar **solo el A2V que falló**, comprueba primero que Postgres lo
marca `failed` por OOM y que no quedan trabajos activos. Detén el Capacity
Controller cuando `ltx25` esté parado, publica una imagen Docker nueva y
prepara únicamente el grupo `ltx25` (no `ltx25_i2v`). Verifica con
`manage_salad_worker.ps1 -Action Status -Service ltx25` el digest
`repositorio@sha256:...` realmente desplegado y escríbelo en `.env`:

```dotenv
SALAD_LTX25_PAGEABLE_A2V_IMAGE=docker.io/yagobordell/ai-video-factory@sha256:DIGEST_REAL
```

Después vuelve a iniciar el controller y ejecuta
`.\\run.ps1 test02 --generate-video`. El runner rechaza imágenes sin el
digest v5. Tras verificar que el fingerprint previo corresponde al mismo
guion, audio, avatar e imágenes, copia al nuevo run los clips locales v4
marcados como completados, comprobando sus hashes, request fingerprints,
metadatos A2V y geometría; **no envía esos clips a Salad otra vez**. Si
alguna salida completada es incompatible o falta, aborta sin regenerarla
silenciosamente. Solo el A2V pendiente 7C obtiene una identidad v5 nueva.
Conserva los manifiestos y costes anteriores y no reinicies B1/B2 completo.

Para revisar un nuevo fallo descarga sus JSON de R2 con
`scripts/salad/inspect_ltx_a2v_diagnostics.py --job-id <ID_NUEVO>`.
La CI prueba el fallback y la reutilización con datos simulados; no se ha
realizado una nueva prueba GPU pagada para v5.

El motivo real del CUDA OOM puede estar en VRAM o en reservas de memoria
fijada del sistema: en anteriores smokes de este repositorio, un OOM ocurrió
incluso con VRAM libre. La comprobación definitiva requiere el traceback y
los eventos `LTX25_A2V_CUDA_MEMORY` de la instancia afectada. La duración
y resolución siguen influyendo en las activaciones; el perfil safe no altera
automáticamente la segmentación de la voz ni introduce cortes visuales.

## Desarrollo

```powershell
uv lock --check
uv run --locked --extra dev python -m pytest
uv run --locked --extra dev ruff check .
```

## Documentación

- [Arquitectura](docs/architecture/overview.md)
- [Auditoría de retirada de bots y preservación de Salad](docs/architecture/planning-retirement-audit.md)
- [Estado de la integración del vídeo](docs/operations/production-runner.md)
- [Configuración de Salad](deploy/salad/services.json)
- [Índice de documentación](docs/README.md)
