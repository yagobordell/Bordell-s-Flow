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

**Fase 4 — Referencias visuales: en desarrollo.** 🚧

Subfase actual: **prompts de referencia visual canónicos implementados; generación de assets pendiente**.

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

6. **Fase 4 — Referencias visuales** 🚧
   - Contrato mínimo `VisualReference = { entity_id, prompt }`.
   - `VisualReferenceBot`: diseño visual canónico por entidad.
   - Plantillas fijas controladas por Python para `character`, `group`, `location` y `object`.
   - Fan-out/fan-in paralelo para entidades independientes.
   - Prompts provider-neutral e inspeccionables antes de generar imágenes.
   - Generación de assets de referencia pendiente.
   - Storyboard grids de hasta 3x3 pendientes y solo cuando aporten valor.

7. **Fase 5 — Audio y timing**
   - TTS.
   - Duraciones reales.
   - STT/alignment para captions.

8. **Fase 6 — Infraestructura GPU**
   - Docker.
   - Salad.
   - Cloudflare R2.
   - Supabase/Postgres.
   - Benchmark de LTX-2.5 antes de fijar hardware y cuantización.

9. **Fase 7 — Generación de vídeo**
   - LTX-2.5 ejecutado directamente desde Python/PyTorch.
   - Jobs reanudables e idempotentes.
   - Sin dependencia de ComfyUI.

10. **Fase 8 — Compositor**
    - Remotion para timeline, transiciones, captions, overlays y motion graphics.
    - FFmpeg/ffprobe para codecs, audio, probing, transcoding y muxing.

11. **Fase 9 — Agentes de verificación**
    - Consistencia narrativa y visual.
    - Verificación técnica.
    - Regeneración selectiva.

La arquitectura detallada está en [`docs/architecture.md`](docs/architecture.md).

## Requisitos

- Python 3.12+
- Git

Para fases posteriores:

- Cuenta/API de OpenAI.
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

Ejecuta los tests:

```bash
python -m pytest
```

## Pipeline de producción

El contrato de entrada es:

```text
SourceScript
    text
```

La jerarquía de planificación es:

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

También puede recibir explícitamente otro archivo de bloques:

```bash
python scripts/run_phase3.py data/output/phase2/narrative_blocks.json
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

La cadena stateful se validó con un ejemplo de tres bloques: un mismo personaje, una misma
localización y un mismo objeto mantuvieron IDs canónicos estables entre turnos.

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

El modelo decide únicamente:

- cómo agrupar beats consecutivos en shots coherentes;
- qué entidades canónicas participan realmente en cada shot;
- una frase breve con la acción visual principal.

Python controla IDs, relaciones con escenas, cobertura de beats y validez de entidades. Cámara,
lente, iluminación, duración, transiciones, estilo y prompts de generación siguen deliberadamente
fuera del contrato.

Validación real completada con el guion de samuráis de la Fase 2: **3 escenas -> 7 shots**, con los
beats **1–11 cubiertos exactamente una vez y en orden**. Los shots combinaron beats consecutivos
cuando formaban una misma acción visual, evitando una fragmentación artificial de un shot por beat.

## Fase 4 — Referencias visuales

La primera subfase consume el registro canónico de entidades de la Fase 3:

```bash
python scripts/run_phase4.py
```

Se puede elegir un estilo visual compartido sin modificar los contratos narrativos:

```bash
python scripts/run_phase4.py --style "cinematic documentary"
```

Genera:

```text
data/output/phase4/visual_references.json
```

Cada referencia conserva el mismo `entity_id` de continuidad y añade únicamente un `prompt`
provider-neutral. `VisualReferenceBot` no escribe libremente el prompt completo: genera una
descripción visual canónica y Python la inserta en una plantilla fija distinta para personajes,
grupos, localizaciones y objetos.

Las entidades se procesan en **paralelo** porque sus identidades ya quedaron resueltas en la Fase 3.
El workflow conserva el orden de entrada y valida que exista exactamente una referencia por cada ID.

Esta subfase produce prompts, no imágenes. La elección e integración del proveedor de generación de
assets se mantiene separada para que podamos inspeccionar y validar primero las identidades visuales
sin acoplar el dominio a una API concreta.

La prueba histórica de la Fase 1 sigue disponible con:

```bash
python scripts/run_phase1.py "La historia de los samuráis"
```
