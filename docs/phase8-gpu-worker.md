# Fase 8.3 — worker GPU de producción

Estado: **cerrada y validada en cloud sobre RTX 5090**.

## Objetivo

La Fase 8.3 conecta el adapter directo de Fase 8.2 al worker idempotente validado en Fase 7 y lo
ejecuta como runtime de producción dentro de Salad Job Queue.

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

Los bytes del vídeo no atraviesan el orquestador. Queue transporta el request y metadata; el MP4
termina bajo `jobs/<application_job_id>/...` en R2.

## Runtime GPU

La imagen conserva la baseline validada en Fase 7:

- PyTorch 2.11.0 + CUDA 12.8;
- Lightricks/LTX-2 commit `a95ab856bf29407b6b066ede0abe1846050db56c`;
- NATTEN 0.21.6;
- LTX-2.5 distilled;
- `fp8-cast`;
- CPU offload;
- conditioning por primer frame;
- MP4 SDR H.264 sin audio.

El task registrado es:

```text
video.ltx25.generate
```

Perfil:

```text
ltx25-distilled-a95ab856-fp8cpu-v1
```

## Recursos cloud validados

```text
Group:       ai-video-factory-worker
Queue:       ai-video-factory-jobs
GPU class:   851399fb-7329-4195-a042-d6514b28cf33  # RTX 5090
CPU:         8
RAM:         61,440 MiB
SHM:         8,192 MiB
Storage:     137,438,953,472 bytes
Priority:    medium
Autoscaler:  min=0, max=1
```

La imagen usada durante el cierre real fue:

```text
docker.io/yagobordell/ai-video-factory@sha256:4577972ab55ecb8fdf305e87d3851b4db6d70b239ed3f61094142a7d7b8d0141
```

La configuración live exitosa alcanzó la versión 11 del Container Group.

## Bootstrap de modelos

La imagen no incorpora los checkpoints grandes. `docker/phase8-worker/download_models.sh` descarga
secuencialmente los cinco ficheros a `/workspace/models/ltx-2.5`:

```text
diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
vae/ltx-2.5-video-vae-bf16.safetensors
vae/ltx-2.5-audio-vae-bf16.safetensors
latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
```

`HF_TOKEN` se suministra únicamente como secreto de entorno. `manage_phase8_worker.ps1 -Action
Prepare` lo obtiene del proceso o mediante `Read-Host -AsSecureString`; nunca se versiona.

El filesystem de una instancia Salad es efímero. Los modelos se reutilizan durante la vida de la
instancia, pero una nueva allocation puede repetir el cold start.

## Health, readiness y liveness

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

`/health` prueba que el proceso está vivo. `/ready` permanece 503 hasta que runtime, Postgres, R2,
modelos, CUDA y pipeline residente están listos.

La validación real mostró que el liveness inicial era demasiado agresivo. Durante construcción del
pipeline/inferencia, la configuración antigua:

```text
timeout_seconds=5
failure_threshold=3
```

podía matar una instancia saludable. El cierre de Fase 8 usa y versiona:

```text
path=/health
period_seconds=30
timeout_seconds=10
failure_threshold=20
success_threshold=1
```

Con esa configuración, la misma instancia permaneció viva durante inferencias reales consecutivas.

## Pipeline residente

`DirectLTX25Backend.prepare()` construye una única `DistilledPipeline`. El backend conserva bindings
y pipeline y serializa `prepare`, `ready` e inferencia con un lock.

Esto amortiza descarga/carga/cuantización durante un batch. El worker validado procesa una inferencia
a la vez, coherente con el perfil RTX 5090 de 32 GiB.

## Gestión del Container Group

```powershell
.\scripts\manage_phase8_worker.ps1 -Action Status
.\scripts\manage_phase8_worker.ps1 -Action Stop
.\scripts\manage_phase8_worker.ps1 -Action Prepare
.\scripts\manage_phase8_worker.ps1 -Action Start
```

`Prepare` solo se usa para una actualización intencional de imagen/configuración y exige el grupo
detenido. Construye/publica la imagen, resuelve el digest, aplica recursos/secretos/probes/Queue y
verifica que Salad activó `priority=medium` y el liveness validado. El grupo permanece detenido al
terminar.

Para un batch normal se usa el grupo ya preparado:

```powershell
.\scripts\start_phase8_autoscaled.ps1
```

Con `min_replicas=0`, la demanda de Queue crea la réplica GPU.

## Smoke real y replay

`scripts/submit_phase8_smoke.py` construye el mismo application job ID canónico que el workflow de
Fase 8.4 mediante `ltx_video_application_job_id()`. Así el smoke y la orquestación completa no pueden
derivar en algoritmos de identidad distintos.

La validación real demostró:

1. shot 1 generado realmente mediante Queue -> worker -> Postgres/R2;
2. H.264 768x1280 a 24 fps, sin audio, descargado y validado por SHA-256;
3. replay exacto del mismo request con `replayed=true` y sin reinferencia;
4. shot 8 y shot 5 ejecutados después sobre la misma instancia caliente;
5. shot 5 completó 233 frames sin OOM;
6. el runtime permaneció estable durante varias inferencias consecutivas;
7. el grupo se devolvió a `stopped` al cerrar la prueba.

Los application job IDs validados y posteriormente reutilizados como replay en Fase 8.4 fueron:

```text
phase8-shot-001-7ed73f1ff682
phase8-shot-005-69262d45109b
phase8-shot-008-a617c67ccd15
```

## Cierre

Fase 8.3 queda cerrada. Fase 8.4 reutilizó exactamente este worker para fanout de los 8 shots y
confirmó que los tres application jobs anteriores replayaban mientras los otros cinco realizaban
inferencia real.

Ver también:

- [`phase8-ltx-adapter.md`](phase8-ltx-adapter.md)
- [`phase8-video-generation.md`](phase8-video-generation.md)
- [`phase8.4-validation-results.md`](phase8.4-validation-results.md)
- [`phase8-closure.md`](phase8-closure.md)
