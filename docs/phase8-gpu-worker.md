# Fase 8.3 — worker GPU de producción

Estado: **cerrada y revalidada en cloud sobre RTX 5090 el 2026-09-15**.

## Objetivo

La Fase 8.3 conecta el adapter directo de Fase 8.2 con el worker idempotente de inferencia y lo ejecuta dentro de Salad Job Queue. El despliegue actual ya no usa el antiguo worker GPU compartido de Fase 8: LTX-2.5 tiene servicio, imagen y cola dedicados.

```text
orquestador local
      ↓
Salad queue: ai-video-factory-ltx25-jobs
      ↓
ai-video-factory-ltx25-worker
      ↓
POST /jobs
      ↓
shared inference worker core
  ├── Postgres: claim / lease / replay
  ├── R2: input / output
  └── video.ltx25.generate
            ↓
     DirectLTX25Backend
            ↓
 DistilledPipeline residente
            ↓
       MP4 silencioso
            ↓
            R2
```

Los bytes del vídeo no atraviesan el orquestador. Queue transporta el request y metadata; el MP4 termina bajo `jobs/<application_job_id>/...` en R2.

## Runtime GPU

La baseline validada conserva:

- PyTorch 2.11.0 + CUDA 12.8;
- Lightricks/LTX-2 commit `a95ab856bf29407b6b066ede0abe1846050db56c`;
- NATTEN 0.21.6;
- LTX-2.5 distilled;
- `fp8-cast`;
- CPU offload;
- conditioning por primer frame;
- MP4 SDR H.264 sin audio.

El task registrado es `video.ltx25.generate` y el perfil validado es `ltx25-distilled-a95ab856-fp8cpu-v1`.

## Recursos cloud validados — servicio dedicado

La revalidación real del 2026-09-15 usó:

```text
Group:       ai-video-factory-ltx25-worker
Queue:       ai-video-factory-ltx25-jobs
Version:     5
GPU class:   851399fb-7329-4195-a042-d6514b28cf33  # RTX 5090 32 GB
CPU:         8
RAM:         40,960 MiB
SHM:         8,192 MiB
Storage:     137,438,953,472 bytes
Priority:    medium
Autoscaler:  min=0, max=4
```

Imagen inmutable validada:

```text
docker.io/yagobordell/ai-video-factory@sha256:598d743b82f75e531cf29c521530a5b9d8d606a6d98a4e7b9fd737811c384a02
```

Esta configuración sustituye como baseline operativo al antiguo slot compartido `ai-video-factory-worker` documentado durante el cierre original de Fase 8. La evidencia histórica del run de 8 shots sigue siendo válida para fanout, replay y estabilidad de la lógica de Fase 8.4; la revalidación de 2026-09-15 confirma la arquitectura dedicada que se despliega actualmente.

## Bootstrap de modelos

La imagen no incorpora los checkpoints grandes. `docker/workers/ltx25/download_models.sh` descarga secuencialmente los cinco ficheros a `/workspace/models/ltx-2.5`:

```text
diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
vae/ltx-2.5-video-vae-bf16.safetensors
vae/ltx-2.5-audio-vae-bf16.safetensors
latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
```

La revalidación descargó y materializó los cinco ficheros sin stall. El downloader publica progreso de bytes cada 30 segundos y aborta una descarga sólo si no observa crecimiento durante 600 segundos. Esto permite distinguir un checkpoint grande pero sano de una transferencia realmente congelada.

`HF_TOKEN` se suministra únicamente como secreto de entorno. El filesystem de la instancia Salad es efímero: los modelos se reutilizan durante la vida de la instancia, pero una nueva allocation puede repetir el cold start.

## Health, readiness y estado busy

El HTTP de salud se desacopla del bootstrap pesado:

```text
container start
  -> FastAPI
  -> /health = 200
  -> download checkpoints
  -> worker.prepare()
  -> construct resident DistilledPipeline
  -> /ready = 200
  -> queue traffic
```

`/health` prueba que el proceso está vivo. `/ready` permanece 503 hasta que runtime, Postgres, R2, modelos, CUDA y pipeline residente están listos.

Durante el smoke real del 2026-09-15 el worker alcanzó `ready=true`, recibió el job y después cambió transitoriamente a `ready=false` mientras la inferencia estaba ocupada. Al completar el job volvió a `ready=true`. Esa transición es comportamiento normal de worker ocupado, no una caída del runtime.

El liveness versionado permanece:

```text
path=/health
period_seconds=30
timeout_seconds=10
failure_threshold=20
success_threshold=1
```

## Queue attachment

La API de Salad no lista de forma fiable un container group detenido en `queue.container_groups`. Durante la revalidación, la observación de attachment seguía siendo falsa incluso después del bootstrap, pero el transporte real recibió el job correctamente y lo ejecutó hasta `succeeded`.

Por tanto, `queue.container_groups` es observabilidad auxiliar y **no** un hard gate de readiness. La prueba autoritativa es que existe una instancia iniciada y ready y que la Queue entrega el trabajo.

## Gestión del Container Group

Los comandos canónicos usan el gestor por servicio:

```powershell
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Status
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Prepare
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Start
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Stop
```

Los wrappers históricos de Fase 8 siguen existiendo por compatibilidad, pero el servicio `ltx25` y su entrada en `deploy/salad/services.json` son la fuente de verdad del despliegue actual.

`Prepare` sólo se usa para una actualización intencional de imagen/configuración y exige el grupo detenido. Construye/publica la imagen, resuelve el digest inmutable y deja el grupo detenido a cero réplicas.

## Smoke real protegido

El comando validado para una prueba real del worker dedicado es:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_ltx25_protected_smoke.ps1 `
  -SkipLocalBuild
```

El wrapper aplica límites separados:

```text
bootstrap total sano:             45 min
running/not-ready reallocation:   60 min
modelo sin progreso de bytes:     10 min
```

Así, una descarga grande que sigue progresando puede terminar sin provocar una reallocation cara, pero una transferencia realmente parada se corta antes. El wrapper ejecuta el `Stop` existente en un bloque `finally`, tanto en éxito como en fallo.

## Evidencia real — 2026-09-15

El smoke generó un shot de un segundo a través de la ruta completa:

```text
application job: phase8-shot-001-730e00d82f95
Salad job:       e06633cb-814e-4e77-99c3-a2b5b0f2f9fd
resolution:      768x1280
fps:             24
frames:          25
seed:            43
replayed:        false
status:          succeeded
```

Artefacto descargado:

```text
data/output/deployment-validation/ltx25-cloud/shot_001.mp4
sha256=ff82d028b04bb5cf91a7198bf75f2e7cf6e585e1cbf25ca8679b376956dc655a
```

La Queue avanzó `pending -> running -> succeeded`, el MP4 fue recuperado desde R2 y el wrapper terminó con:

```text
status=stopped
replicas=0
pending=False
```

La evidencia detallada está en [`ltx25-salad-validation-2026-09-15.md`](ltx25-salad-validation-2026-09-15.md).

## Evidencia histórica de Fase 8.4

Antes de la migración al worker dedicado, Fase 8.4 validó fanout completo, resume, replay y múltiples inferencias secuenciales sobre el antiguo slot de producción. Esa evidencia no se invalida por el cambio de despliegue: las identidades `phase8-shot-*`, los contratos de Postgres/R2 y el workflow de video permanecen compatibles.

Ver [`phase8.4-validation-results.md`](phase8.4-validation-results.md) para el run completo de 8 shots.

## Cierre

Fase 8.3 queda cerrada también sobre la arquitectura dedicada actual. La ruta validada es:

```text
orchestrator -> ai-video-factory-ltx25-jobs -> dedicated LTX worker
             -> Postgres/R2 -> direct LTX inference -> MP4 -> R2 -> local verification
```

El siguiente trabajo sobre LTX debe tratar optimización de cold start como un cambio separado de la baseline de correctness ya probada.
