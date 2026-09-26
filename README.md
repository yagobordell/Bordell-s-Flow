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

Completa `.env` con las credenciales y configuración necesarias. Los secretos y los artefactos
generados no deben versionarse. Python se instala desde `uv.lock`; cualquier cambio de dependencias
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

Fish Audio utiliza el documento de instrucciones
`src/ai_video_factory/bots/prompts/fish_audio.md`, copiado del BotFish
adjunto. Empieza **a la vez que B1.1**, recibe el mismo guion sin modificar
y solo inserta etiquetas entre corchetes. Su salida se envía como `text`
al endpoint TTS de OpenSpeaker con la voz
`fishaudio_f8dfe9c83081432386f143e2fe9767ef`. La generación guarda
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
# Finish after B2, without generating images or invoking AI33/OpenAI image APIs:
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt --avatar monje.png --no-image
# Continue existing paid AI33 image tasks without repeating any OpenAI bot:
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt --images-only
```

En una terminal interactiva, sin argumentos, el runner ofrece primero el
menú de guiones y después el de avatares. Si eliges el guion con `--script`,
puedes elegir el avatar en el segundo menú; para automatizarlo especifica
ambos argumentos. El runner comprueba que el avatar es un PNG válido antes
de generar y copia sus bytes a `data/output/<nombre-del-guion>/avatar.png`.

Cada guion escribe en `data/output/<nombre-del-guion>/`: input/output de
B1.1, output por bloque y merge de B1.2/B2, `visual_plan.json` y
`run_report.json` con costes, tiempos y metadatos. Ambos JSON incluyen el
avatar elegido, su imagen local de la ejecución, dimensiones y SHA-256; el
modelo B2 sigue recibiendo y devolviendo sus contratos originales. Para repetir
solo etapas posteriores, usa `--from B1.2` o `--from B2`; el runner valida y
reutiliza los artefactos previos del mismo guion. La consola
imprime una línea de tiempo y coste inmediatamente después de terminar cada bot.

Al terminar B2, el runner genera automáticamente **una imagen PNG por beat con
`description` no vacía** mediante AI33 Pro/OpenSpeaker: Flare, 16:9, 1K y
calidad low por defecto. Configura `AI33_API_KEY` en `.env` y utiliza
`AI33_IMAGE_MODEL=gpt-image-2.5-sunburst` si deseas cambiar a Sunburst.
Los beats `avatar` con descripción nula no generan PNG. Las imágenes y sus
costes en créditos se guardan en `images/` dentro del output del guion. Si
se interrumpe una tarea, `--images-only` recupera los `task_id` guardados
sin volver a generar ni repetir los bots. Si una tarea AI33 conocida supera
30 minutos desde su envío, se activa el fallback de la API oficial de OpenAI
con el **mismo modelo y prompt**, calidad low, PNG y tamaño mínimo 16:9
(`1280x720`). Requiere `OPENAI_API_KEY` y se controla con
`OPENAI_IMAGE_FALLBACK_ENABLED=true`. El ID de AI33 permanece registrado:
su tarea podría completarse más tarde y ocasionar también un cargo allí.
`--no-image` (alias de `--skip-images`) permite ejecutar solo B1.1/B1.2/B2
mientras iteras los prompts: se conservan los outputs de B2 y
`visual_plan.json`, sin generar imágenes ni llamar a AI33/OpenAI Images.

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
