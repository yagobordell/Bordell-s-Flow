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

**Fase 2 — Narrative planning: en validación.**

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

4. **Fase 2 — Narrative planning** 🚧
   - `NarrativeBlockBot`: guion -> bloques narrativos.
   - `BeatExtractorBot`: bloques -> beats, en paralelo.
   - `ScenePlannerBot`: beats -> escenas.
   - IDs asignados por código determinista, no por el modelo.
   - Validaciones contra pérdida, duplicación o reordenación de contenido.

5. **Fase 3 — Continuidad y shots**
   - Procesamiento stateful bloque a bloque.
   - Asignación de personajes y escenarios.
   - Planificación de shots escena a escena.

6. **Fase 4 — Referencias visuales**
   - Referencias consistentes de personajes y escenarios.
   - Plantillas visuales fijas.
   - Storyboard grids de hasta 3x3 cuando aporten valor.

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

La jerarquía narrativa es:

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
```

Los primeros contratos se mantienen deliberadamente pequeños:

```text
NarrativeBlock = { id, text }
Beat           = { id, block_id, action }
Scene          = { id, beat_ids }
```

El generador de guion de la Fase 1 sigue disponible como utilidad opcional:

```text
Topic -> ScriptWriterAgent -> Script -> SourceScript
```

## Ejecutar la Fase 2

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

`BeatExtractorBot` se ejecuta en paralelo para todos los bloques narrativos. El orden y los IDs
de los beats se reconstruyen después de forma determinista antes de llamar a `ScenePlannerBot`.

La prueba histórica de la Fase 1 sigue disponible con:

```bash
python scripts/run_phase1.py "La historia de los samuráis"
```
