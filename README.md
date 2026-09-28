# AI Video Factory

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
configura `SALAD_ORGANIZATION_NAME`, `SALAD_PROJECT_NAME` y `SALAD_API_KEY` con los datos
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
al endpoint TTS de OpenSpeaker con la voz
`fishaudio_80e34d5e0b2b4577a486f3a77e357261`. La generación guarda
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
`historia_roma.txt`) y las imágenes PNG de avatar en `data/avatar/` (por ejemplo,
`monje.png`). Las imágenes PNG y los guiones son locales y están ignorados por Git;
en `data/avatar/README.md` están las instrucciones para esta biblioteca.

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
`data/avatar/.qr/<nombre-avatar.png>/`, de modo que las siguientes
generaciones con el mismo avatar lo reutilizan. También se guarda una copia
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
