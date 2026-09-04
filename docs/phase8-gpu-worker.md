# Fase 8.3 — worker GPU de producción

Estado: **implementada en código; pendiente de validación cloud en RTX 5090 antes del cierre**.

## Objetivo

La Fase 8.2 incorporó el adaptador directo `LTXVideoTaskRunner -> DirectLTX25Backend ->
DistilledPipeline`. La Fase 8.3 lo conecta al worker idempotente validado en Fase 7 y construye una
imagen de producción capaz de ejecutar `video.ltx25.generate` en SaladCloud.

La ruta completa queda:

```text
Salad Job Queue
      ↓
POST /jobs
      ↓
GPUWorker
  ├── Postgres: claim / lease / replay
  ├── R2: input / output
  └── TaskRunnerRegistry.phase8
            ↓
     LTXVideoTaskRunner
            ↓
     DirectLTX25Backend
            ↓
 DistilledPipeline residente
            ↓
       MP4 silencioso
            ↓
            R2
```

Los bytes del vídeo no atraviesan el orquestador. Queue recibe únicamente metadata y el MP4
termina bajo `jobs/<job_id>/...` en R2.

## Selección de runtime

`GPU_WORKER_RUNTIME` separa los dos modos:

```text
phase7 -> infrastructure.copy
phase8 -> infrastructure.copy + video.ltx25.generate
```

El valor por defecto permanece `phase7`, de modo que entornos locales y tests no necesitan Torch,
CUDA ni LTX. La imagen `docker/phase8-worker/Dockerfile` fija `GPU_WORKER_RUNTIME=phase8`.

## Runtime GPU fijado

La imagen conserva la baseline validada en Fase 7:

- `pytorch/pytorch:2.11.0-cuda12.8-cudnn9-devel`;
- Torch `2.11.0`, torchvision `0.26.0`, torchaudio `2.11.0`;
- Lightricks/LTX-2 commit `a95ab856bf29407b6b066ede0abe1846050db56c`;
- NATTEN `0.21.6+torch2110cu128`;
- LTX-2.5 distilled;
- `fp8-cast`;
- CPU offload;
- CUDA;
- first-frame conditioning, frame 0, strength `1.0`;
- MP4 SDR H.264 sin audio.

El worker de Salad HTTP Job Queue sigue fijado en `v0.7.0` y validado por SHA-256, igual que en la
Fase 7.

## Recursos cloud

La configuración de validación reutiliza la clase de RTX 5090 ya comprobada:

```text
GPU class: 851399fb-7329-4195-a042-d6514b28cf33
CPU:       8
RAM:       61,440 MiB
SHM:       8,192 MiB
Storage:   137,438,953,472 bytes
```

El grupo sigue siendo `ai-video-factory-worker` y la Queue `ai-video-factory-jobs`.

El lease de aplicación se amplía a 900 segundos para cubrir clips más largos que el benchmark de
121 frames. El heartbeat permanece en 30 segundos.

## Bootstrap de modelos

La imagen no contiene los aproximadamente 66 GiB de checkpoints. Al iniciar una instancia,
`phase8-download-models` descarga secuencialmente los cinco archivos de `Lightricks/LTX-2.5` a:

```text
/workspace/models/ltx-2.5
```

Cada archivo existente y no vacío se reutiliza dentro de la vida de la instancia. Después del
download se vuelve a comprobar que los cinco ficheros existen y tienen tamaño positivo.

El filesystem de una instancia Salad es efímero. Por tanto esta política es reproducible pero no
asume persistencia entre reallocations. Reducir ese cold-start mediante una distribución de pesos
más eficiente es una optimización posterior y no altera los contratos de Fase 8.

## Warmup y readiness

El orden de arranque es deliberado:

```text
container start
  -> download/checkpoints
  -> start FastAPI
  -> lifespan: worker.prepare()
  -> CUDA + model validation
  -> construct DistilledPipeline
  -> /ready = 200
  -> start salad-http-job-queue-worker
```

La Queue no empieza a consumir trabajos hasta que el pipeline está construido y residente. Así la
carga fría no consume el lease de un job ya reclamado.

`/ready` comprueba en cada llamada:

- Postgres;
- R2;
- presencia de los cinco modelos;
- CUDA disponible;
- pipeline ya preparado.

La configuración Salad usa Startup, Readiness y Liveness probes. El Startup probe dispone de un
margen amplio para download y warmup. Readiness es más estricta una vez completado el startup.

## Pipeline residente

`DirectLTX25Backend.prepare()` construye una sola instancia de `DistilledPipeline`. El backend
conserva tanto los bindings LTX como el pipeline y serializa `prepare`, `ready` e inferencia con un
lock.

Esto evita cargar y cuantizar los modelos para cada shot. Un proceso ejecuta una única inferencia a
la vez, coherente con la baseline RTX 5090 de 32 GiB.

## Imagen y gestión del Container Group

La utilidad operativa es:

```powershell
.\scripts\manage_phase8_worker.ps1 -Action Status
.\scripts\manage_phase8_worker.ps1 -Action Stop
.\scripts\manage_phase8_worker.ps1 -Action Prepare
.\scripts\manage_phase8_worker.ps1 -Action Start
```

`Prepare`:

1. exige que el grupo esté detenido;
2. construye y publica `docker/phase8-worker/Dockerfile` para `linux/amd64`;
3. resuelve el digest publicado y configura Salad con la referencia inmutable;
4. aplica recursos, secretos, probes, Queue y autoscaler;
5. deja el grupo detenido y con cero réplicas.

Nunca se deben compartir ni versionar los valores de `POSTGRES_DSN`, `SALAD_API_KEY` o credenciales
R2.

## Smoke real

`scripts/submit_phase8_smoke.py` toma los artefactos canónicos existentes:

```text
data/output/phase6/storyboard_keyframes.json
data/output/phase8/video_prompts.json
data/output/phase5/shot_timings.json
```

Para un shot:

1. localiza el PNG canónico;
2. calcula SHA-256;
3. calcula `num_frames` con la rejilla `8k + 1`;
4. construye un `job_id` determinista a partir del plan completo;
5. sube el keyframe a R2;
6. envía `video.ltx25.generate` a Salad Job Queue;
7. espera el resultado;
8. descarga el MP4 desde R2;
9. verifica su SHA-256;
10. si `ffprobe` está disponible, exige un stream de vídeo y cero streams de audio.

Ejemplo:

```powershell
python scripts\submit_phase8_smoke.py --shot-id 1
```

Ejecutar exactamente el mismo comando otra vez produce el mismo application `job_id` y permite
validar replay idempotente. Si la instancia sigue viva, el replay no vuelve a ejecutar LTX.

## Gate de cierre de Fase 8.3

La subfase no se considera cerrada hasta reunir evidencia cloud de:

- Ruff y pytest en verde;
- build/push de la imagen GPU;
- Container Group actualizado por digest inmutable;
- instancia sobre la clase RTX 5090 validada;
- bootstrap correcto de los cinco checkpoints;
- `/ready` solo después de pipeline residente;
- una generación real de shot 1 mediante Queue -> worker -> Postgres/R2;
- MP4 H.264 no vacío, sin audio, descargado y verificado por SHA-256;
- replay del mismo request sin reinferencia y sin incrementar `attempt_count`;
- una generación del shot real más largo (shot 5, 233 frames) sin OOM;
- al menos dos inferencias reales consecutivas sobre la misma instancia sin error de runtime;
- Container Group detenido al terminar la validación.

La medición fina de fan-out, reanudación del vídeo completo y reconstrucción `VideoClip[]` pertenece a
Fase 8.4.
