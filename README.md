# AI Video Factory

Pipeline educativo para generar vídeos cortos verticales a partir de un guion mediante bots
estructurados, providers desacoplados y workflows reproducibles.

El objetivo principal no es únicamente producir un vídeo, sino aprender una arquitectura de
producción con contratos pequeños, artefactos persistidos, fan-out/fan-in, procesamiento stateful,
media providers, GPU remota y composición programática.

## Estado

**Fase 7 — Infraestructura GPU: implementación completa; validación cloud pendiente.** 🟡

Última fase cerrada: **Fase 6 — Storyboard y planificación visual por shot**. ✅
Las entregas 7.1 y 7.2 están implementadas. Falta ejecutar la matriz en GPU real y validar el
despliegue con las cuentas de R2, Supabase y Salad antes de cerrar operativamente la fase.

La Fase 1 queda conservada como experimento funcional. El pipeline de producción definitivo
empieza desde un **guion ya terminado**, no desde un tema.

## Roadmap

1. **Fase 0 — Base del proyecto** ✅
   - Estructura Python.
   - Configuración mediante variables de entorno.
   - Contratos Pydantic.
   - Tests y Ruff.
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
   - IDs asignados por Python.
   - El modelo decide fronteras; Python reconstruye estructura canónica.
   - Validaciones contra pérdida, duplicación o reordenación.

5. **Fase 3 — Continuidad y shots** ✅
   - Provider stateful basado en `previous_response_id`.
   - `ContinuityBot`: procesamiento serial bloque a bloque.
   - Registro canónico de personajes, grupos, lugares y objetos físicos.
   - IDs de entidad asignados por Python.
   - `ShotPlannerBot`: planificación serial escena a escena.
   - Cobertura exacta y ordenada de beats en los shots.

6. **Fase 4 — Referencias visuales** ✅
   - `VisualReference = { entity_id, prompt }`.
   - `VisualReferenceBot`: diseño visual canónico por entidad.
   - Contexto derivado de narrativa + continuidad.
   - `ImageProvider` desacoplado del dominio.
   - `ReferenceAsset = { entity_id, uri }`.
   - Assets PNG con nombres deterministas.

7. **Fase 5 — Audio y timing** ✅
   - Narración TTS continua desde `SourceScript.text`.
   - WAV validado y duración medida desde PCM real.
   - Alignment por palabra.
   - `BeatTimingBot`: modelo decide fronteras de palabra.
   - `BeatTiming[]` reconstruido por Python.
   - `ShotTiming[]` derivado sin LLM.
   - Timeline continua de principio a fin.

8. **Fase 6 — Storyboard y planificación visual por shot** ✅
   - `StoryboardFrame = { shot_id, prompt }`.
   - Prompts seriales por escena usando acción, referencias y duración real.
   - `StoryboardKeyframe = { shot_id, uri }`.
   - Generación vertical condicionada por referencias visuales canónicas.
   - Un keyframe independiente por shot para evitar propagación de errores.
   - `StoryboardGrid = { scene_id, uri }`.
   - Grids por escena compuestos localmente con Pillow, sin IA.
   - Validación real de 8 keyframes y 3 grids con el ejemplo de samuráis.

9. **Fase 7 — Infraestructura GPU** 🟡
   - Benchmark reproducible de LTX-2.5 implementado; ejecución real pendiente.
   - Worker HTTP stateless e idempotente con contratos versionados.
   - Cloudflare R2 para inputs/outputs con SHA-256 y reconciliación.
   - Supabase/Postgres para estado transaccional, leases y reintentos.
   - Docker con Salad Job Queue Worker `v0.7.0` fijado por checksum.
   - Queue, autoscaling, readiness, manifiestos y smoke test end-to-end.
   - Hardware y cuantización se fijarán únicamente después del benchmark real.

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

La Fase 6.3 añade Pillow como dependencia de runtime para componer storyboard grids localmente.

Para la Fase 7.1:

- Entorno Linux con GPU NVIDIA y `nvidia-smi`.
- Instalación oficial de LTX-2.5 y sus checkpoints en el nodo de benchmark.

Para fases posteriores:

- FFmpeg.
- Node.js para Remotion.
- Docker.
- Acceso a GPU remota.

## Instalación local

Con `uv`:

```bash
uv sync --extra dev
```

Para trabajar con la infraestructura de Fase 7:

```bash
uv sync --extra dev --extra gpu
```

O con `pip`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

En Windows PowerShell:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
```

Copia `.env.example` a `.env` y añade ahí tus claves reales.

## Seguridad y datos locales

- Nunca subas `.env` al repositorio.
- `.env` y `.env.*` están ignorados por Git.
- Solo `.env.example` se versiona y debe contener valores vacíos o de ejemplo.
- Las claves reales deben permanecer únicamente en local o en un gestor de secretos.
- Los guiones colocados en `data/input/` se ignoran y no se versionan.
- Los artefactos de `data/output/` y `data/tmp/` tampoco se versionan.

## Pipeline de producción

Entrada canónica:

```text
SourceScript
  text
```

Jerarquía narrativa:

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

Continuidad visual:

```text
ContinuityEntity[]
  ↓
VisualReference[]
  ↓
ReferenceAsset[]
```

Jerarquía temporal:

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

Storyboard implementado:

```text
Shot[] + ShotTiming[] + VisualReference[]
                  ↓
          StoryboardFrame[]
                  ↓
ReferenceAsset[] + StoryboardFrame[]
                  ↓
          StoryboardKeyframe[]
                  ↓
Scene[] + Shot[] + StoryboardKeyframe[]
                  ↓
            StoryboardGrid[]
```

## Contratos canónicos

Los contratos se mantienen deliberadamente pequeños:

```text
NarrativeBlock    = { id, text }
Beat              = { id, block_id, action }
Scene             = { id, beat_ids }
ContinuityEntity  = { id, kind, name, description }
BlockContinuity   = { block_id, entity_ids }
Shot              = { id, scene_id, beat_ids, entity_ids, action }
VisualReference   = { entity_id, prompt }
ReferenceAsset    = { entity_id, uri }
NarrationAudio    = { uri, duration_seconds }
NarrationWord     = { id, text, start_seconds, end_seconds }
BeatTiming        = { beat_id, start_word_id, end_word_id, start_seconds, end_seconds }
ShotTiming        = { shot_id, start_seconds, end_seconds }
StoryboardFrame   = { shot_id, prompt }
StoryboardKeyframe = { shot_id, uri }
StoryboardGrid    = { scene_id, uri }
```

Los parámetros de provider, modelo, calidad, resolución y composición no se duplican en los
contratos persistidos salvo que sean necesarios para el siguiente stage.

La Fase 7.1 añade contratos de evidencia de infraestructura separados del dominio audiovisual:

```text
GPUDeviceProfile    = { index, name, memory_total_mib }
LTXBenchmarkProfile = { label, ltx_source, pipeline, quantization, offload, dimensions, runs }
LTXBenchmarkSample  = { run_index, duration_seconds, peak_gpu_memory_mib, output metadata }
LTXBenchmarkReport  = { profile, command, devices, samples, aggregate metrics }
```

## Fase 2 — Narrative planning

Guarda un guion final en un archivo local, por ejemplo:

```text
data/input/script.txt
```

Ejecuta:

```bash
python scripts/run_phase2.py data/input/script.txt
```

Salida:

```text
data/output/phase2/
├── source_script.json
├── narrative_blocks.json
├── beats.json
└── scenes.json
```

`NarrativeBlockBot` decide fronteras y Python reconstruye texto inmutable. `BeatExtractorBot` se
ejecuta en paralelo. `ScenePlannerBot` agrupa beats consecutivos después de que Python haya
restaurado IDs globales deterministas.

Validación real: 1 bloque narrativo, 11 beats y 3 escenas contiguas.

## Fase 3 — Continuidad y shots

Continuidad:

```bash
python scripts/run_phase3.py
```

Shots:

```bash
python scripts/run_phase3_shots.py
```

Salida principal:

```text
data/output/phase3/
├── entities.json
├── block_continuity.json
└── shots.json
```

`ContinuityBot` y `ShotPlannerBot` usan cadenas stateful separadas. La continuidad entre stages se
transfiere mediante IDs y JSON canónicos, no mediante memoria oculta compartida.

La validación histórica de Fase 3 produjo 7 shots; una regeneración posterior produjo 8. Los stages
downstream no dependen de una cantidad fija de shots.

## Fase 4 — Referencias visuales

Prompts canónicos:

```bash
python scripts/run_phase4.py
```

Assets:

```bash
python scripts/run_phase4_assets.py --quality medium
```

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

La validación real produjo tres referencias reutilizables y tres PNG coherentes con el Japón
feudal del ejemplo.

## Fase 5 — Audio y timing

```bash
python scripts/run_phase5_audio.py
python scripts/run_phase5_alignment.py --language es
python scripts/run_phase5_beat_timing.py
python scripts/run_phase5_shot_timing.py
```

Salida:

```text
data/output/phase5/
├── narration.wav
├── narration.json
├── narration_words.json
├── beat_timings.json
└── shot_timings.json
```

Validación real:

- WAV canónico de **45.0 s**.
- **105** palabras alineadas.
- **11** beats temporizados cubriendo `0.0–45.0 s`.
- **8** shots temporizados cubriendo `0.0–45.0 s` sin huecos ni solapes.

## Fase 6 — Storyboard y planificación visual

### 6.1 Prompts de storyboard

```bash
python scripts/run_phase6_storyboard.py
```

Inputs principales:

```text
Shot[]
ShotTiming[]
VisualReference[]
```

`StoryboardFrameBot` procesa los shots en orden dentro de cada escena. Recibe duración real y las
referencias canónicas de las entidades del shot. El prompt anterior se usa únicamente como contexto
de continuidad dentro de la misma escena; al cambiar de escena se reinicia.

El modelo decide composición estática, pero el output persistido sigue siendo mínimo:

```text
StoryboardFrame = { shot_id, prompt }
```

Las instrucciones obligan a representar el núcleo de `Shot.action`, evitan que continuidad se
convierta en repetición y traducen ideas abstractas como legado o memoria a evidencia visual
concreta.

### 6.2 Keyframes reales

```bash
python scripts/run_phase6_keyframes.py
```

Por defecto se generan PNG verticales de `1024x1536`.

Cada shot recibe únicamente los `ReferenceAsset` correspondientes a sus `entity_ids`. Cuando hay
referencias, `OpenAIImageProvider` usa generación condicionada por imágenes; cuando no las hay,
recurre a generación desde texto.

Los keyframes se generan de forma independiente y concurrente. No se encadena el frame anterior
como imagen de entrada para evitar propagar errores visuales entre shots.

Persistencia:

```text
StoryboardKeyframe = { shot_id, uri }
```

Salida:

```text
data/output/phase6/
├── storyboard_frames.json
├── storyboard_keyframes.json
└── storyboard_keyframes/
    ├── shot_001.png
    ├── shot_002.png
    ├── ...
    └── shot_008.png
```

### 6.3 Storyboard grids por escena

```bash
python scripts/run_phase6_storyboard_grids.py
```

Esta etapa **no usa IA ni API externa**. Pillow agrupa los keyframes por `Shot.scene_id`, conserva
el orden, mantiene el aspect ratio y compone hojas de contacto adaptativas de hasta tres columnas.

Persistencia:

```text
StoryboardGrid = { scene_id, uri }
```

Salida completa de Fase 6:

```text
data/output/phase6/
├── storyboard_frames.json
├── storyboard_keyframes.json
├── storyboard_keyframes/
│   ├── shot_001.png
│   ├── ...
│   └── shot_008.png
├── storyboard_grids.json
└── storyboard_grids/
    ├── scene_001.png
    ├── scene_002.png
    └── scene_003.png
```

Validación real con el guion de samuráis:

- **8** `StoryboardFrame` revisados semánticamente.
- **8** keyframes verticales de `1024x1536` generados con referencias canónicas.
- Identidad visual consistente sin forzar la misma composición en todos los shots.
- El shot final traduce el legado a armadura, katana y representación pictórica en vez de repetir
  una pose de guerreros vivos.
- **3** grids deterministas: escena 1 con shots 1–4, escena 2 con shots 5–6 y escena 3 con shots 7–8.
- Grids sin recorte de keyframes, con orden y etiquetas verificables.

Con esta validación, **Fase 6 queda cerrada**.

## Fase 7 — Infraestructura GPU

### 7.1 Benchmark reproducible de LTX-2.5

La primera entrega ya implementa el harness que debe ejecutarse sobre cada GPU candidata antes de
fijar el worker de Salad:

```bash
python scripts/run_phase7_benchmark.py \
  --label l40s-distilled-fp8-cpu \
  --pipeline distilled \
  --quantization fp8-cast \
  --offload cpu \
  --width 768 \
  --height 1280 \
  --num-frames 121 \
  --fps 24 \
  --warmup-runs 1 \
  --measured-runs 3 \
  -- \
  python -m ltx_pipelines.distilled ... --output-path '{output}'
```

El workflow separa warmups, muestrea VRAM con `nvidia-smi`, valida cada MP4 y persiste hashes,
tiempos, throughput y pico de memoria por GPU. El comando se ejecuta sin shell y sus credenciales
comunes se redactan antes de escribir el informe.

Salida:

```text
data/output/phase7/
└── ltx_benchmark.json
```

La guía y la matriz inicial de perfiles están en
[`docs/phase7-benchmark.md`](docs/phase7-benchmark.md).

### 7.2 Worker remoto idempotente

La infraestructura desplegable está implementada:

```text
orchestrator
    ↓
Salad Job Queue
    ↓
Docker + Salad worker v0.7.0
    ↓
HTTP POST /jobs
    ↙                 ↘
R2 GET inputs      R2 PUT outputs
    ↘                 ↙
Supabase/Postgres leases + state
```

El payload incluye un `job_id` de aplicación, claves R2 deterministas y SHA-256 opcionales para
inputs. Postgres une de forma inmutable el ID al fingerprint del request. Un heartbeat renueva el
lease durante trabajos largos. Si un nodo cae después del upload pero antes del commit, el siguiente
intento reconcilia los metadatos de R2 y no repite el trabajo.

`infrastructure.copy` valida el recorrido completo sin pagar GPU. El runner LTX real sigue
perteneciendo a Fase 8 y se registrará en el mismo boundary después de elegir hardware con evidencia.

Guía completa, comandos y criterios de cierre:
[`docs/phase7-deployment.md`](docs/phase7-deployment.md).

La implementación está completa, pero la fase permanece abierta hasta ejecutar la matriz real,
publicar la imagen y conservar un smoke test exitoso en las cuentas de R2, Supabase y Salad.

## Compatibilidad de Fase 1

La prueba histórica de Fase 1 sigue disponible:

```bash
python scripts/run_phase1.py "La historia de los samuráis"
```

`DirectorAgent` y `StoryboardScene` permanecen como experimento/regresión y no forman parte del
pipeline de producción actual.
