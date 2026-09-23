# AI Video Factory

Pipeline educativo para generar vídeos cinematográficos 16:9 a partir de un guion mediante bots
estructurados, providers desacoplados y workflows reproducibles.

El objetivo no es únicamente producir un vídeo: el proyecto sirve para construir y validar una
arquitectura de producción con contratos pequeños, artefactos persistidos, fan-out/fan-in,
procesamiento stateful, media providers, GPU remota y composición programática.

## Estado

**Fase 9 — Compositor.** ✅

Última fase cerrada: **Fase 9 — Compositor**. El pipeline ya transforma los ocho clips H.264 generados
en Fase 8, la timeline canónica de Fase 5 y los timings por palabra en un vídeo audiovisual final de
45 segundos con captions, resaltado de palabra activa, transiciones, motion overlays y narración AAC.

El contrato de producción de esta rama es:

```text
LTX:             1280x720 @ 24 fps
Real-ESRGAN:     2560x1440 @ 24 fps
Phase 9/final:   2560x1440 @ 24 fps
H.264 + AAC
```

La validación vertical 768x1280 permanece como baseline histórico. La validación real Salad del
nuevo recorrido 16:9 y su replay sin GPU siguen siendo gates obligatorios antes de marcar este PR
como ready o fusionarlo.

El artefacto audiovisual final es:

```text
data/output/phase9/final_video.mp4
```

con contrato downstream:

```text
FinalVideo = { uri, duration_seconds }
```

Siguiente fase: **Fase 10 — Agentes de verificación**.

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
    - Producción migrada a clips LTX H.264 1280×720, 24 fps; la validación cloud nueva está pendiente.
    - Fan-in canónico a `VideoClip[]` con tamaño y SHA-256 verificados.

11. **Fase 9 — Compositor** ✅
    - Timeline frame-exact derivada de `ShotTiming[]` sin drift acumulado.
    - Captions deterministas desde `NarrationWord[]`, sin IA adicional.
    - Remotion aislado para render visual, captions y motion overlays.
    - Transiciones que preservan los límites canónicos de los 8 shots.
    - FFmpeg/ffprobe para probing y mux final sin recodificar el vídeo aceptado.
    - Compositor migrado a 2560×1440 @ 24 fps; frame count/timeline se preservan y el mux final sigue H.264 + AAC.

12. **Fase 10 — Agentes de verificación**
    - Consistencia narrativa y visual.
    - Verificación técnica y regeneración selectiva.

La arquitectura detallada está en [`docs/architecture.md`](docs/architecture.md).
El cierre técnico de Fase 8 está en [`docs/phase8-closure.md`](docs/phase8-closure.md).
El cierre formal de Fase 9 está en [`docs/phase9-closure.md`](docs/phase9-closure.md).

## Requisitos

- Python 3.12+
- Git
- Cuenta/API de OpenAI para las fases que usan modelos hospedados.
- Pillow para composición local de storyboard grids.
- FFmpeg/ffprobe para validación de media y mux final.
- Node.js 22+ para el renderer aislado de Remotion.
- Docker para build/prepare/smokes de workers; no es necesario para el run end-to-end normal con workers Salad ya preparados.
- Acceso a Salad, Cloudflare R2 y Supabase/Postgres para la ruta cloud.

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

Instalación del renderer Remotion en Windows PowerShell:

```powershell
Push-Location .\remotion
npm.cmd install
Pop-Location
```

## Seguridad y datos locales

- Nunca subas `.env` al repositorio.
- `.env` y `.env.*` están ignorados; solo `.env.example` se versiona.
- Claves de Salad, R2, Postgres, Hugging Face y OpenAI deben vivir únicamente en entorno local o
  gestores de secretos.
- Los guiones de `data/input/` y artefactos de `data/output/`/`data/tmp/` no se versionan.
- Los scripts PowerShell piden secretos mediante `Read-Host -AsSecureString` cuando no existen ya en
  el entorno del proceso.

## Ejecución end-to-end optimizada

El camino normal de producción es un único comando en Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\run_video_factory.ps1 `
    -Input .\data\input\script.txt `
    -NonInteractive
```

Este runner ejecuta preflight global **antes de reservar GPU**, resuelve cache/resume, planifica el
pipeline como DAG con concurrencia acotada, controla prewarm/cleanup de Salad, aplica fallbacks
clasificados, genera los clips, ejecuta Remotion/FFmpeg y termina en:

```text
data/output/phase9/final_video.mp4
```

Las optimizaciones no cambian modelos, revisiones, resolución, fps, seeds, perfiles de generación ni
contratos audiovisuales. El ahorro viene de dependencias reales, solapamiento seguro, replay
determinista antes de GPU y reducción de cold starts evitables.

Puntos clave:

- Breeze puede arrancar/trabajar en paralelo con la rama de continuidad;
- shots y prompts de referencias se desacoplan cuando sus dependencias lo permiten;
- prompts de movimiento y keyframes parten en paralelo desde el storyboard;
- Phase 4 y Phase 6 comparten Qwen-Image-2.1 sin ejecutarse simultáneamente sobre el recurso limitado;
- Ideogram se conserva como ruta explícita para futuras tareas y alternativas controladas;
- Breeze, keyframes y LTX auditan R2 antes de prewarm/submission;
- Fish no se prewarmea de forma especulativa y sigue consumiendo cero GPU-seconds si Breeze funciona;
- Qwen-Image-2.1 es el único proveedor activo de imagen en Phase 4 y Phase 6; el auditor de R2 sólo
  precalienta cuando detecta trabajo Qwen no cacheado. Ideogram queda implementado para uso futuro, pero fuera del flujo activo;
- el `finally` end-to-end detiene los servicios del proyecto, aplica guard de replicas=0 y limpia
  las queues.

Métricas de cada run:

```text
data/output/preflight_report.json
data/output/production_metrics.json
data/output/video_factory_metrics.json
```

Consulta [`docs/production-runner.md`](docs/production-runner.md) y
[`docs/end-to-end-performance.md`](docs/end-to-end-performance.md) para el DAG, políticas de
recursos/cache y protocolo de benchmark.

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
StoryboardFrame[] + Shot[]
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
              VideoClip[] 1280x720
                         ↓
          Real-ESRGAN_x2plus worker
                         ↓
              VideoClip[] 2560x1440
```

Composición final:

```text
Upscaled VideoClip[] + ShotTiming[] + NarrationWord[]
                    ↓
          composition_plan.json
                    ↓
        Remotion visual renderer
                    ↓
             visual_motion.mp4
                    ↓
NarrationAudio + FFmpeg final mux
                    ↓
                FinalVideo
```

Handoff actual a Fase 10:

```text
FinalVideo
  uri
  duration_seconds
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
FinalVideo         = { uri, duration_seconds }
```

Queue IDs, hashes, retries, provider parameters, Remotion props y respuestas de GPU pertenecen al
estado operativo o de render, no al contrato audiovisual final.

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
python scripts/run_phase4_assets.py --quality high
```

### Fase 5 — Audio y timing

En producción, la narración usa Breeze TTS 2 como proveedor principal y Fish Speech S2 Pro
self-hosted en Salad como fallback conservador. Fish solo se enciende ante fallos terminales
clasificados de Breeze y usa una referencia de voz autorizada persistida en R2.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\run_phase5_audio_controlled.ps1 `
    -SourceFile .\data\output\phase2\source_script.json `
    -OutputDir .\data\output\phase5 `
    -Metadata .\data\output\phase5\narration.json `
    -NonInteractive
```

Después:

```bash
python scripts/run_phase5_alignment.py --language es
python scripts/run_phase5_beat_timing.py
python scripts/run_phase5_shot_timing.py
```

La validación real produjo narración canónica de 45.0 s, 105 palabras alineadas, 11 beats y 8 shots
cubriendo la timeline completa sin huecos ni solapes. El fallback Fish también ha sido validado en
Salad con referencia condicionada, replay desde R2 a replicas=0 y cleanup final.

### Fase 6 — Storyboard

```bash
python scripts/run_phase6_storyboard.py
python scripts/run_phase6_keyframes.py
python scripts/run_phase6_storyboard_grids.py
```

El camino de producción de esta rama genera keyframes y grids landscape 16:9; la validación vertical anterior queda como baseline histórico.

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
ltx25-distilled-a95ab856-fp8cpu-gridpad-v2
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

La corrida real histórica de cierre produjo 8 clips H.264 a 768×1280 y 24 fps. En esta rama ese
baseline queda invalidado por identidad: LTX genera 1280×720 @ 24 fps y después una etapa separada
Real-ESRGAN_x2plus genera clips 2560×1440. La nueva validación cloud completa todavía es obligatoria
antes del merge.

Salida canónica:

```text
data/output/phase8/
├── video_prompts.json
├── video_generation_manifest.json
├── video_clips.json
├── video_clips/                 # LTX 1280x720
├── video_upscale_manifest.json
├── upscaled_clips.json
└── upscaled_clips/              # Real-ESRGAN 2560x1440
```

Documentación:

- [`docs/phase8-video-prompts.md`](docs/phase8-video-prompts.md)
- [`docs/phase8-ltx-adapter.md`](docs/phase8-ltx-adapter.md)
- [`docs/phase8-gpu-worker.md`](docs/phase8-gpu-worker.md)
- [`docs/phase8-video-generation.md`](docs/phase8-video-generation.md)
- [`docs/phase8.4-validation-results.md`](docs/phase8.4-validation-results.md)
- [`docs/phase8-closure.md`](docs/phase8-closure.md)

Con esta validación, **Fase 8 queda cerrada**.

## Fase 9 — Compositor

Phase 9 mantiene `ShotTiming` como fuente de verdad y convierte toda la composición a una timeline
frame-exact de 1080 frames a 24 fps.

### 9.1 Timeline y media probe

`ffprobe` valida cada clip de Fase 8 y Python cuantiza límites absolutos a frames sin redondear
duraciones de shots de forma independiente.

Intervalos canónicos:

```text
shot 1:    0 ->   84
shot 2:   84 ->  261
shot 3:  261 ->  383
shot 4:  383 ->  450
shot 5:  450 ->  683
shot 6:  683 ->  841
shot 7:  841 -> 1018
shot 8: 1018 -> 1080
```

### 9.2 Captions deterministas

`NarrationWord[]` se convierte en 29 cues y 105 palabras renderizables sin llamadas de IA. Los IDs,
el orden y la evidencia temporal canónica se preservan.

### 9.3–9.4 Renderer visual

Remotion vive en un proyecto Node/TypeScript aislado. `Phase9Visual` conserva el baseline hard-cut y
`Phase9Motion` añade transiciones internas a cada shot y acentos de corte sin mover los límites
canónicos. Subtítulos y barra superior están desactivados por defecto; se habilitan explícitamente
con `--show-captions` y `--show-progress-bar`.

Los artefactos de transcripción `†el` y `sirve†` se normalizan sólo para presentación como `el` y
`sirve`; la transcripción canónica no se muta.

### 9.5 Mux final

FFmpeg combina `visual_motion.mp4` con el WAV de narración. El vídeo se mantiene con `-c:v copy` y
sólo el audio se codifica a AAC.

La validación demostró que el elementary stream H.264 antes y después del mux tiene el mismo SHA-256,
por lo que no existe una nueva generación de vídeo en el paso final.

Reproducción del compositor completo:

```powershell
python scripts/run_phase9_compositor.py
python scripts/run_phase9_remotion.py
python scripts/run_phase9_motion.py
python scripts/run_phase9_final.py
```

Salida final:

```text
data/output/phase9/
├── composition_plan.json
├── visual.mp4
├── visual_motion.mp4
├── final_video.mp4
└── final_video.json
```

Documentación:

- [`docs/phase9-compositor.md`](docs/phase9-compositor.md)
- [`docs/phase9.3-remotion.md`](docs/phase9.3-remotion.md)
- [`docs/phase9.4-motion.md`](docs/phase9.4-motion.md)
- [`docs/phase9.5-final-mux.md`](docs/phase9.5-final-mux.md)
- [`docs/phase9-closure.md`](docs/phase9-closure.md)

Con la validación audiovisual y técnica del artefacto final, **Fase 9 queda formalmente cerrada**.

## Compatibilidad de Fase 1

La prueba histórica sigue disponible:

```bash
python scripts/run_phase1.py "La historia de los samuráis"
```

`DirectorAgent` y `StoryboardScene` permanecen únicamente como experimento/regresión y no forman
parte del pipeline de producción actual.
