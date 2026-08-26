# AI Video Factory

Pipeline educativo para generar vídeos cortos verticales a partir de un guion mediante bots
estructurados, workers GPU y composición programática.

Objetivos principales:

- Aprender orquestación de workflows, bots y agentes de verificación.
- Usar OpenAI Responses API + Structured Outputs para planificación narrativa.
- Ejecutar modelos open-source de vídeo en GPU remota con Docker.
- Aprender fan-out/fan-in, procesamiento stateful, retries e idempotencia.
- Generar y almacenar artefactos intermedios de forma reproducible.
- Montar el resultado con Remotion y FFmpeg.

## Estado

**Fase 5 — Audio y timing: completada.** ✅

Siguiente fase formal: **Fase 6 — Storyboard y planificación visual por shot**.

La Fase 1 queda conservada como experimento funcional. El pipeline de producción definitivo
empieza desde un **guion ya terminado**, no desde un tema.

## Roadmap revisado

1. **Fase 0 — Base del proyecto** ✅
   - Estructura Python.
   - Configuración mediante variables de entorno.
   - Contratos Pydantic.
   - Tests.
   - Git.

2. **Fase 1 — OpenAI + Structured Outputs** ✅
   - Guionista experimental.
   - Director experimental.
   - Provider desacoplado.
   - Workflow ejecutable con salida JSON.

3. **Fase 1.5 — Refactor de arquitectura** ✅
   - El guion pasa a ser la entrada canónica de producción.
   - Bots acotados en lugar de agentes autónomos.
   - Contratos mínimos: `NarrativeBlock`, `Beat` y `Scene`.
   - El Director antiguo se mantiene solo como compatibilidad/experimento.

4. **Fase 2 — Narrative planning** ✅
   - `NarrativeBlockBot`: guion -> bloques narrativos.
   - `BeatExtractorBot`: bloques -> beats, en paralelo.
   - `ScenePlannerBot`: beats -> escenas.
   - IDs asignados por código determinista, no por el modelo.
   - Segmentación por fronteras: el modelo decide cortes y Python reconstruye el texto original.
   - Validaciones contra pérdida, duplicación o reordenación de contenido.
   - Workflow validado con una ejecución real contra OpenAI.

5. **Fase 3 — Continuidad y shots** ✅
   - Provider stateful basado en `previous_response_id`.
   - `ContinuityBot`: procesamiento serial bloque a bloque.
   - Registro canónico de personajes, grupos, lugares y objetos físicos.
   - Persistencia de entidades contextualmente activas entre bloques.
   - IDs de entidad asignados por Python (`character_001`, `location_001`, etc.).
   - Referencias de entidades por bloque mediante `BlockContinuity`.
   - `ShotPlannerBot`: planificación serial escena a escena.
   - IDs de shot globales y deterministas asignados por Python.
   - Cobertura exacta y ordenada de beats en los shots.
   - Validación real multi-turn de continuidad y validación real de shot planning.

6. **Fase 4 — Referencias visuales** ✅
   - Contrato mínimo `VisualReference = { entity_id, prompt }`.
   - `VisualReferenceBot`: diseño visual canónico por entidad.
   - Contexto narrativo derivado de `NarrativeBlock` + `BlockContinuity`.
   - Plantillas fijas para `character`, `group`, `location` y `object`.
   - Localizaciones amplias convertidas en un entorno físico único y contextual.
   - Fan-out/fan-in paralelo para referencias independientes.
   - `ImageProvider` desacoplado del dominio.
   - `OpenAIImageProvider` como implementación inicial.
   - Contrato mínimo `ReferenceAsset = { entity_id, uri }`.
   - PNGs con nombres deterministas por `entity_id`.
   - Persistencia del lote solo después de completar todas las generaciones.
   - Validación real de prompts y assets con el guion de samuráis.

7. **Fase 5 — Audio y timing** ✅
   - Narración TTS continua desde `SourceScript.text`.
   - `SpeechProvider` desacoplado y `OpenAISpeechProvider` inicial.
   - WAV validado y duración medida desde los frames PCM reales.
   - `TranscriptionProvider` para timestamps por palabra.
   - `NarrationWord[]` como evidencia temporal sin sustituir el guion canónico.
   - `BeatTimingBot`: el modelo decide solo fronteras semánticas por palabra.
   - `BeatTiming[]` reconstruido determinísticamente por Python.
   - `ShotTiming[]` derivado sin LLM a partir de `Shot.beat_ids`.
   - Timeline final continua y validada de principio a fin.

8. **Fase 6 — Storyboard y planificación visual por shot**
   - Keyframe representativo por shot.
   - Acción + entidades canónicas + duración real como contexto.
   - Prompts visuales provider-neutral antes de generar imágenes.
   - Evaluar grids por escena a partir de keyframes individuales.

9. **Fase 7 — Infraestructura GPU**
   - Docker.
   - Salad.
   - Cloudflare R2.
   - Supabase/Postgres.
   - Benchmark de LTX-2.5 antes de fijar hardware y cuantización.

10. **Fase 8 — Generación de vídeo**
    - LTX-2.5 ejecutado directamente desde Python/PyTorch.
    - Jobs reanudables e idempotentes.
    - Sin dependencia de ComfyUI.

11. **Fase 9 — Compositor**
    - Remotion para timeline, transiciones, captions, overlays y motion graphics.
    - FFmpeg/ffprobe para codecs, audio, probing, transcoding y muxing.

12. **Fase 10 — Agentes de verificación**
    - Consistencia narrativa y visual.
    - Verificación técnica.
    - Regeneración selectiva.

La arquitectura detallada está en [`docs/architecture.md`](docs/architecture.md).

## Requisitos

- Python 3.12+
- Git
- Cuenta/API de OpenAI para las fases que usan modelos hospedados.

Para fases posteriores:

- FFmpeg.
- Node.js para Remotion.
- Acceso a GPU remota.

## Instalación local

Con `uv`:

```bash
uv sync --dev
```

O con `pip`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

En Windows PowerShell, si el entorno no está activado, también puedes usar directamente:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest
```

Copia `.env.example` a `.env` y añade ahí tus claves reales.

## Seguridad y datos locales

- Nunca subas `.env` al repositorio.
- `.env` y `.env.*` están ignorados por Git.
- Solo `.env.example` se versiona y debe contener valores vacíos o de ejemplo.
- Las claves reales deben permanecer únicamente en local o en un gestor de secretos.
- Los guiones colocados en `data/input/` se ignoran y no se versionan.
- Los artefactos de `data/output/` y `data/tmp/` tampoco se versionan.

Ejecuta los tests y lint:

```bash
python -m pytest
python -m ruff check .
```

## Pipeline de producción

El contrato de entrada es:

```text
SourceScript
    text
```

La jerarquía narrativa y visual implementada es:

```text
SourceScript
  ↓
NarrativeBlock[]
  ↓
Beat[]
  ↓
Scene[]
  ↓
Shot[]

ContinuityEntity[]
  ↓
VisualReference[]
  ↓
ReferenceAsset[]
```

La jerarquía temporal implementada es:

```text
SourceScript.text
  ↓
NarrationAudio
  ↓
NarrationWord[]
  ↓
BeatTiming[]
  ↓
ShotTiming[]
```

Los contratos se mantienen deliberadamente pequeños:

```text
NarrativeBlock   = { id, text }
Beat             = { id, block_id, action }
Scene            = { id, beat_ids }
ContinuityEntity = { id, kind, name, description }
BlockContinuity  = { block_id, entity_ids }
Shot             = { id, scene_id, beat_ids, entity_ids, action }
VisualReference  = { entity_id, prompt }
ReferenceAsset   = { entity_id, uri }
NarrationAudio   = { uri, duration_seconds }
NarrationWord    = { id, text, start_seconds, end_seconds }
BeatTiming       = { beat_id, start_word_id, end_word_id, start_seconds, end_seconds }
ShotTiming       = { shot_id, start_seconds, end_seconds }
```

El generador de guion de la Fase 1 sigue disponible como utilidad opcional:

```text
Topic -> ScriptWriterAgent -> Script -> SourceScript
```

## Fase 2 — Narrative planning

Guarda un guion final en un archivo local, por ejemplo:

```text
data/input/script.txt
```

Después ejecuta:

```bash
python scripts/run_phase2.py data/input/script.txt
```

La Fase 2 genera:

```text
data/output/phase2/
├── source_script.json
├── narrative_blocks.json
├── beats.json
└── scenes.json
```

`NarrativeBlockBot` decide fronteras entre unidades del guion y Python reconstruye los bloques
a partir del texto original, evitando reescrituras accidentales. `BeatExtractorBot` se ejecuta en
paralelo para todos los bloques narrativos. El orden y los IDs de los beats se reconstruyen después
de forma determinista antes de llamar a `ScenePlannerBot`.

Validación real completada con un guion corto: 1 bloque narrativo, 11 beats y 3 escenas contiguas.
Un guion corto puede formar un único bloque si desarrolla una sola unidad temática; no se fuerzan
cortes artificiales únicamente para crear paralelismo.

## Fase 3 — Continuidad stateful

La continuidad consume los bloques narrativos generados por la Fase 2:

```bash
python scripts/run_phase3.py
```

Genera:

```text
data/output/phase3/
├── entities.json
└── block_continuity.json
```

`ContinuityBot` se ejecuta de forma **serial**. Cada llamada recibe el registro canónico acumulado
y continúa el estado de la respuesta anterior mediante `previous_response_id`. Las instrucciones se
envían de nuevo en cada turno y Python mantiene la fuente de verdad para IDs y relaciones.

El modelo solo decide qué entidades existentes reaparecen y qué nuevas entidades visualmente
relevantes aparecen. Python asigna después IDs deterministas por tipo, por ejemplo:

```text
character_001
group_001
location_001
object_001
```

Las entidades contextualmente activas pueden mantenerse aunque el siguiente bloque no repita su
nombre, siempre que la narración continúe claramente en el mismo lugar o situación. `object` se
reserva para objetos físicos tangibles: conceptos como honor, disciplina o bushido no reciben IDs
de continuidad.

## Fase 3 — Shot planning

Los shots consumen los beats y escenas de la Fase 2 junto con los artefactos de continuidad:

```bash
python scripts/run_phase3_shots.py
```

Genera:

```text
data/output/phase3/shots.json
```

`ShotPlannerBot` procesa las escenas en orden y mantiene su propia cadena stateful independiente de
`ContinuityBot`. La continuidad pasa entre etapas mediante IDs y JSON canónicos, no mediante memoria
oculta compartida entre bots.

El modelo decide cómo agrupar beats consecutivos, qué entidades participan realmente y una acción
visual breve. Python controla IDs, relaciones con escenas, cobertura de beats y validez de entidades.

La validación histórica de cierre de Fase 3 produjo **3 escenas -> 7 shots**, con los beats **1–11
cubiertos exactamente una vez y en orden**. Una regeneración posterior usada para validar Fase 5
produjo 8 shots; los workflows downstream no dependen de una cantidad fija de shots.

## Fase 4 — Referencias visuales

### 1. Prompts canónicos

La primera parte consume entidades, bloques narrativos y continuidad por bloque:

```bash
python scripts/run_phase4.py
```

Se puede elegir un estilo visual compartido:

```bash
python scripts/run_phase4.py --style "cinematic documentary"
```

Genera:

```text
data/output/phase4/visual_references.json
```

`VisualReferenceBot` genera una descripción visual estable por entidad y Python la inserta en una
plantilla fija según `character`, `group`, `location` u `object`.

El contexto de cada entidad se deriva únicamente de los `NarrativeBlock` donde aparece en
`BlockContinuity`. Esto permite resolver época y entorno sin convertir acciones temporales en rasgos
permanentes. Las localizaciones demasiado amplias se concretan como un único entorno físico
representativo en vez de una descripción geográfica enciclopédica o un collage de épocas.

Las referencias se procesan en paralelo y conservan el mismo ID canónico:

```text
VisualReference = { entity_id, prompt }
```

### 2. Assets de referencia

Después de inspeccionar los prompts, las imágenes se generan de forma explícita:

```bash
python scripts/run_phase4_assets.py --quality medium
```

El modelo de imagen puede configurarse mediante `OPENAI_IMAGE_MODEL` o `--model`.

La frontera del provider es independiente del dominio:

```text
VisualReference[]
      ↓
ImageProvider
      ↓
GeneratedImage[]   # bytes efímeros
      ↓
ReferenceAsset[]   # metadata persistida
```

Los bytes no se guardan dentro del JSON. El workflow espera a que todas las generaciones terminen
antes de escribir el lote, y usa nombres deterministas basados en `entity_id`.

Salida:

```text
data/output/phase4/
├── visual_references.json
├── reference_assets.json
└── reference_assets/
    ├── group_001.png
    ├── group_002.png
    └── location_001.png
```

Contrato persistido:

```text
ReferenceAsset = { entity_id, uri }
```

Validación real completada con el guion de samuráis:

- `group_001`: referencia visual de samuráis en armadura.
- `group_002`: referencia visual diferenciada de señores feudales.
- `location_001`: entorno físico coherente del Japón feudal, sin apariencia moderna injustificada.
- 3 prompts canónicos produjeron 3 PNG utilizables.
- `reference_assets.json` conservó los tres IDs con URIs deterministas.

Con esta validación, **Fase 4 queda cerrada**.

## Fase 5 — Audio y timing

### 1. Narración canónica

La narración se genera como una única pista continua directamente desde `SourceScript.text`:

```bash
python scripts/run_phase5_audio.py
```

Genera:

```text
data/output/phase5/
├── narration.wav
└── narration.json
```

El provider devuelve bytes efímeros y el workflow valida el WAV, mide la duración real y solo
después persiste el archivo. La medición usa los frames PCM realmente presentes para soportar WAV
streaming con tamaños sentinel en el header.

```text
NarrationAudio = { uri, duration_seconds }
```

### 2. Alignment por palabra

```bash
python scripts/run_phase5_alignment.py --language es
```

Genera:

```text
data/output/phase5/narration_words.json
```

`NarrationWord.text` es evidencia reconocida, no una nueva fuente de verdad narrativa. El guion
canónico sigue siendo `SourceScript.text`. Se permiten timestamps puntuales donde
`start_seconds == end_seconds`, siempre que la secuencia global permanezca ordenada y dentro de la
duración medida.

### 3. Timing de beats

```bash
python scripts/run_phase5_beat_timing.py
```

`BeatTimingBot` decide únicamente qué `NarrationWord.id` termina cada beat. Python reconstruye
rangos de palabras e intervalos contiguos. La pausa entre dos beats se asigna al beat anterior, de
modo que el siguiente cambio visual coincide con el comienzo de la siguiente idea hablada.

```text
BeatTiming = { beat_id, start_word_id, end_word_id, start_seconds, end_seconds }
```

### 4. Timing de shots

```bash
python scripts/run_phase5_shot_timing.py
```

No usa LLM ni API externa. Cada shot hereda el inicio de su primer beat y el final de su último
beat, después de validar cobertura exacta y ordenada.

```text
ShotTiming = { shot_id, start_seconds, end_seconds }
```

Validación real de Fase 5 con el guion de samuráis:

- Narración WAV válida de **45.0 s**.
- **105** palabras alineadas desde `0.0` hasta `44.58 s`.
- **11** `BeatTiming` consecutivos cubriendo exactamente `0.0–45.0 s`.
- La regeneración de shots usada en esta fase produjo **8** `ShotTiming` consecutivos.
- Los 8 shots cubren exactamente `0.0–45.0 s`, sin huecos ni solapes.
- La derivación temporal no depende de un número fijo de shots.

Con esta validación, **Fase 5 queda cerrada**.

La prueba histórica de la Fase 1 sigue disponible con:

```bash
python scripts/run_phase1.py "La historia de los samuráis"
```
