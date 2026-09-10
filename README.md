# AI Video Factory

Pipeline educativo para generar vídeos cortos verticales a partir de un guion mediante bots
estructurados, providers desacoplados y workflows reproducibles.

El objetivo no es únicamente producir un vídeo: el proyecto sirve para construir y validar una
arquitectura de producción con contratos pequeños, artefactos persistidos, fan-out/fan-in,
procesamiento stateful, media providers, GPU remota y composición programática.

## Estado

**Fase 8 — Generación de vídeo con LTX-2.5.** ✅

Última fase cerrada: **Fase 8 — Generación de vídeo**. El pipeline ya transforma los keyframes y
prompts de movimiento de cada shot en clips MP4 reales mediante LTX-2.5 ejecutado directamente desde
Python/PyTorch sobre un worker RTX 5090 en Salad. La corrida canónica de 8 shots validó fanout,
resume, replay idempotente, verificación SHA-256 y fan-in a `VideoClip[]`.

Siguiente fase: **Fase 9 — Compositor**.

La Fase 1 se conserva como experimento funcional. El pipeline de producción definitivo empieza desde
un **guion ya terminado**, no desde un tema.

## Roadmap

1. **Fase 0 — Base del proyecto** ✅
   - Estructura Python, configuración, contratos Pydantic, tests, Ruff y Git.

2. **Fase 1 — OpenAI + Structured Outputs** ✅
   - Guionista y director experimentales.

3. **Fase 1.5 — Refactor de arquitectura** ✅
   - El guion final pasa a ser la entrada canónica.
   - Bots acotados y contratos narrativos mínimos.

4. **Fase 2 — Narrative planning** ✅
   - `NarrativeBlockBot`, `BeatExtractorBot` y `ScenePlannerBot`.
   - IDs y reconstrucción estructural controlados por Python.

5. **Fase 3 — Continuidad y shots** ✅
   - `ContinuityBot` stateful y `ShotPlannerBot` serial por escena.
   - Registro canónico de entidades físicas y cobertura exacta de beats.

6. **Fase 4 — Referencias visuales** ✅
   - `VisualReferenceBot`, `ReferenceAsset[]` y generación condicionada por identidad.

7. **Fase 5 — Audio y timing** ✅
   - Narración TTS continua, alignment por palabra, `BeatTiming[]` y `ShotTiming[]`.

8. **Fase 6 — Storyboard y planificación visual por shot** ✅
   - `StoryboardFrame[]`, keyframes independientes y grids deterministas por escena.

9. **Fase 7 — Infraestructura GPU** ✅
   - Benchmark real LTX-2.5 en RTX 5090.
   - Worker HTTP idempotente, Cloudflare R2, Supabase/Postgres y Salad Job Queue.
   - Imagen digest-pinned y baseline `fp8-cast` + CPU offload.

10. **Fase 8 — Generación de vídeo** ✅
    - Motion prompts separados de la composición estática.
    - Adapter directo LTX-2.5 Python/PyTorch; sin ComfyUI.
    - Worker GPU real con runtime residente.
    - Fanout completo, resume desde manifiesto y replay idempotente.
    - 8 clips H.264 reales validados a 768×1280, 24 fps.
    - Fan-in canónico a `VideoClip[]` con tamaño y SHA-256 verificados.

11. **Fase 9 — Compositor**
    - Remotion para timeline, transiciones, captions, overlays y motion graphics.
    - FFmpeg/ffprobe para probing, transcoding, audio y muxing final.

12. **Fase 10 — Agentes de verificación**
    - Consistencia narrativa y visual.
    - Verificación técnica y regeneración selectiva.

La arquitectura detallada está en [`docs/architecture.md`](docs/architecture.md).
El cierre técnico completo de Fase 8 está en [`docs/phase8-closure.md`](docs/phase8-closure.md).

## Requisitos

- Python 3.12+
- Git
- Cuenta/API de OpenAI para las fases que usan modelos hospedados.
- Pillow para composición local de storyboard grids.
- FFmpeg/ffprobe para validación de media y, desde Fase 9, composición/muxing.
- Docker para los workers GPU.
- Acceso a Salad, Cloudflare R2 y Supabase/Postgres para la ruta cloud.
- Node.js será necesario en Fase 9 para Remotion.

Instalación local con `uv`:

```bash
uv sync --extra dev
```

Para infraestructura GPU:

```bash
uv sync --extra dev --extra gpu
```

En Windows PowerShell:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
```

## Seguridad y datos locales

- Nunca subas `.env` al repositorio.
- `.env` y `.env.*` están ignorados; solo `.env.example` se versiona.
- Claves de Salad, R2, Postgres, Hugging Face y OpenAI deben vivir únicamente en entorno local o
  gestores de secretos.
- Los guiones de `data/input/` y artefactos de `data/output/`/`data/tmp/` no se versionan.
- Los scripts PowerShell piden secretos mediante `Read-Host -AsSecureString` cuando no existen ya en
  el entorno del proceso.

## Pipeline de producción

Entrada canónica:

```text
SourceScript
  text
```

Planificación narrativa:

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

Storyboard:

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

Vídeo generado:

```text
StoryboardKeyframe[] + Shot[] + ShotTiming[]
                         ↓
                    VideoPrompt[]
                         ↓
       deterministic GPUJobRequest[]
                         ↓
             Salad Job Queue + R2
                         ↓
            direct LTX-2.5 worker
                         ↓
                    VideoClip[]
```

Handoff previsto a Fase 9:

```text
VideoClip[] + ShotTiming[] + NarrationAudio + NarrationWord[]
                              ↓
                       compositor final
```

## Contratos canónicos

Los contratos de dominio se mantienen deliberadamente pequeños:

```text
NarrativeBlock     = { id, text }
Beat               = { id, block_id, action }
Scene              = { id, beat_ids }
ContinuityEntity   = { id, kind, name, description }
BlockContinuity    = { block_id, entity_ids }
Shot               = { id, scene_id, beat_ids, entity_ids, action }
VisualReference    = { entity_id, prompt }
ReferenceAsset     = { entity_id, uri }
NarrationAudio     = { uri, duration_seconds }
NarrationWord      = { id, text, start_seconds, end_seconds }
BeatTiming         = { beat_id, start_word_id, end_word_id, start_seconds, end_seconds }
ShotTiming         = { shot_id, start_seconds, end_seconds }
StoryboardFrame    = { shot_id, prompt }
StoryboardKeyframe = { shot_id, uri }
StoryboardGrid     = { scene_id, uri }
VideoPrompt        = { shot_id, prompt }
VideoClip          = { shot_id, uri }
```

Queue IDs, hashes, retries, provider parameters y respuestas de GPU pertenecen al estado operativo,
no al contrato audiovisual final.

## Fases 2–6

### Fase 2 — Narrative planning

```bash
python scripts/run_phase2.py data/input/script.txt
```

Salida principal:

```text
data/output/phase2/
├── source_script.json
├── narrative_blocks.json
├── beats.json
└── scenes.json
```

### Fase 3 — Continuidad y shots

```bash
python scripts/run_phase3.py
python scripts/run_phase3_shots.py
```

Salida principal:

```text
data/output/phase3/
├── entities.json
├── block_continuity.json
└── shots.json
```

### Fase 4 — Referencias visuales

```bash
python scripts/run_phase4.py
python scripts/run_phase4_assets.py --quality medium
```

### Fase 5 — Audio y timing

```bash
python scripts/run_phase5_audio.py
python scripts/run_phase5_alignment.py --language es
python scripts/run_phase5_beat_timing.py
python scripts/run_phase5_shot_timing.py
```

La validación real produjo narración canónica de 45.0 s, 105 palabras alineadas, 11 beats y 8 shots
cubriendo la timeline completa sin huecos ni solapes.

### Fase 6 — Storyboard

```bash
python scripts/run_phase6_storyboard.py
python scripts/run_phase6_keyframes.py
python scripts/run_phase6_storyboard_grids.py
```

La validación real produjo 8 keyframes verticales y 3 grids de escena.

## Fase 7 — Infraestructura GPU

La infraestructura cloud conserva este boundary:

```text
orchestrator
    ↓
Salad Job Queue
    ↓
Docker worker
    ↙                 ↘
R2 inputs/outputs   Supabase/Postgres leases + state
```

El benchmark real de cierre en RTX 5090 obtuvo una media de **194.93 s** para 121 frames a
768×1280, con **24,513 MiB** de pico de VRAM usando `fp8-cast` + CPU offload.

Documentación:

- [`docs/phase7-benchmark.md`](docs/phase7-benchmark.md)
- [`docs/phase7-deployment.md`](docs/phase7-deployment.md)
- [`docs/phase7-closure.md`](docs/phase7-closure.md)

## Fase 8 — Generación de vídeo

### 8.1 Motion prompts

`VideoPromptBot` transforma `Shot[]`, `ShotTiming[]` y contexto visual en una instrucción de movimiento
por shot sin modificar el keyframe canónico.

```text
VideoPrompt = { shot_id, prompt }
```

### 8.2 Adapter directo LTX-2.5

El task de producción es:

```text
video.ltx25.generate
```

Perfil validado:

```text
ltx25-distilled-a95ab856-fp8cpu-v1
```

Las duraciones de `ShotTiming` se redondean al siguiente frame count válido de LTX (`8k + 1`) sin
cambiar la timeline canónica.

### 8.3 Worker GPU real

El worker conserva el contrato de Fase 7 y añade LTX como task real. El pipeline se carga una vez y
permanece residente mientras la réplica consume jobs secuencialmente.

La validación real demostró inferencia, replay idempotente y varios clips generados sobre la misma
instancia caliente.

### 8.4 Fanout, resume y fan-in

```powershell
# Con el worker detenido: fanout completo.
python scripts\run_phase8_videos.py --submit-only

# Habilitar grupo autoscalado existente.
.\scripts\start_phase8_autoscaled.ps1

# Resume/poll/fan-in. Muestra cambios de estado del manifiesto en vivo.
python scripts\run_phase8_videos.py

# Apagar tras completar el batch.
.\scripts\manage_phase8_worker.ps1 -Action Stop
```

Un manifiesto atómico conserva application job ID, request SHA, transport ID, estado, número de
submissions y respuesta validada. Un rerun de un manifest completo no consulta ni resubmite los
jobs ya exitosos.

La corrida real de cierre produjo 8 clips H.264 a 768×1280 y 24 fps. Los shots 1, 5 y 8 fueron
replays de jobs previos; los demás realizaron inferencia nueva. Una segunda ejecución terminó con
cero nuevas submissions y cero transport IDs nuevos.

Salida canónica:

```text
data/output/phase8/
├── video_prompts.json
├── video_generation_manifest.json
├── video_clips.json
└── video_clips/
    ├── shot_001.mp4
    ├── ...
    └── shot_008.mp4
```

Documentación:

- [`docs/phase8-video-prompts.md`](docs/phase8-video-prompts.md)
- [`docs/phase8-ltx-adapter.md`](docs/phase8-ltx-adapter.md)
- [`docs/phase8-gpu-worker.md`](docs/phase8-gpu-worker.md)
- [`docs/phase8-video-generation.md`](docs/phase8-video-generation.md)
- [`docs/phase8.4-validation-results.md`](docs/phase8.4-validation-results.md)
- [`docs/phase8-closure.md`](docs/phase8-closure.md)

Con esta validación, **Fase 8 queda cerrada**.

## Compatibilidad de Fase 1

La prueba histórica sigue disponible:

```bash
python scripts/run_phase1.py "La historia de los samuráis"
```

`DirectorAgent` y `StoryboardScene` permanecen únicamente como experimento/regresión y no forman
parte del pipeline de producción actual.
