# Fase 8.2 — adaptador directo LTX-2.5

Estado: **cerrada en código y CI; la inferencia GPU real se valida al activar el runtime en Fase 8.3**.

## Objetivo

La Fase 8.1 decide el movimiento semántico de cada shot mediante `VideoPrompt`. La Fase 8.2 define
cómo una petición GPU concreta convierte ese prompt y un keyframe en un clip MP4 usando
LTX-2.5 directamente desde Python/PyTorch, sin ComfyUI y sin invocar el CLI mediante subprocess.

La frontera queda así:

```text
StoryboardKeyframe + VideoPrompt + parámetros de generación
                         ↓
                    GPUJobRequest
                         ↓
                  LTXVideoTaskRunner
                         ↓
                  DirectLTX25Backend
                         ↓
             DistilledPipeline (residente)
                         ↓
                   MP4 sin audio
```

## Task y perfil canónicos

Task:

```text
video.ltx25.generate
```

Perfil:

```text
ltx25-distilled-a95ab856-fp8cpu-v1
```

El perfil fija la baseline validada en Fase 7:

- upstream Lightricks/LTX-2 commit `a95ab856bf29407b6b066ede0abe1846050db56c`;
- pipeline `DistilledPipeline`;
- checkpoints split de LTX-2.5;
- cuantización `fp8-cast`;
- CPU offload;
- first-frame image conditioning en frame 0 con strength 1.0;
- ejecución CUDA;
- salida SDR H.264 MP4.

El job debe incluir el `generation_profile` explícitamente. De esta forma un cambio futuro de
checkpoint, cuantización, offload o política de inferencia cambia también el fingerprint del job y
no puede ser confundido con una generación anterior.

## Parámetros del job

`LTXVideoParameters` acepta exactamente:

```json
{
  "generation_profile": "ltx25-distilled-a95ab856-fp8cpu-v1",
  "prompt": "...",
  "seed": 43,
  "width": 768,
  "height": 1280,
  "fps": 24,
  "num_frames": 121
}
```

Reglas:

- no se aceptan campos adicionales;
- `generation_profile` debe coincidir exactamente con el perfil canónico;
- prompt no vacío;
- width y height divisibles por 64 para el pipeline two-stage;
- `num_frames` debe cumplir `8k + 1`;
- el output debe ser `.mp4` con `content_type=video/mp4`;
- debe existir exactamente un input llamado `keyframe`.

## Duración y rejilla temporal

La duración narrativa sigue perteneciendo a `ShotTiming`. Para construir el job, el helper
`ltx_num_frames_for_duration()` convierte esa duración a frames y redondea **hacia arriba** al
primer valor válido `8k + 1`.

Con los timings reales del ejemplo de samuráis a 24 FPS:

```text
shot 1  3.50 s ->  89 frames
shot 2  7.38 s -> 185 frames
shot 3  5.06 s -> 129 frames
shot 4  2.82 s ->  73 frames
shot 5  9.68 s -> 233 frames
shot 6  6.60 s -> 161 frames
shot 7  7.38 s -> 185 frames
shot 8  2.58 s ->  65 frames
```

El clip generado nunca queda por debajo de la duración solicitada por el redondeo. La Fase 9 puede
recortar con precisión al `ShotTiming` canónico durante la composición.

## Pipeline residente

`DirectLTX25Backend` construye `DistilledPipeline` de forma lazy en la primera generación y conserva
esa instancia durante toda la vida del proceso. Las siguientes tareas reutilizan el mismo pipeline.

La inferencia se protege con un lock y se serializa dentro del proceso. El objetivo es evitar:

- recargar aproximadamente 66 GiB de checkpoints por shot;
- ejecutar dos inferencias simultáneas sobre una RTX 5090 de 32 GiB;
- introducir carreras sobre los componentes y caches del pipeline.

La prueba unitaria verifica que dos generaciones consecutivas construyen una sola instancia del
pipeline.

## Modelos esperados

El backend recibe un `model_root` y espera exactamente el layout ya utilizado en el benchmark de
Fase 7:

```text
<model_root>/
├── diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
├── text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
├── vae/ltx-2.5-video-vae-bf16.safetensors
├── vae/ltx-2.5-audio-vae-bf16.safetensors
└── latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
```

Los cinco archivos se validan antes de construir el pipeline.

## Política de audio

LTX-2.5 genera también audio internamente, pero no es el audio canónico del proyecto.

El adaptador llama a `encode_video(..., audio=None, ...)`, por lo que el artefacto de Fase 8 es un
MP4 deliberadamente sin pista de audio. `data/output/phase5/narration.wav` sigue siendo la fuente
canónica y se muxeará en la fase de composición.

## Separación con Fase 8.3

Esta subfase no modifica todavía `gpu/runtime.py` ni la imagen de producción. El runtime actual
sigue registrando `TaskRunnerRegistry.phase7()`.

Fase 8.3 debe:

1. construir una imagen GPU que combine el worker idempotente de Fase 7 con el runtime Torch/LTX
   validado por el benchmark;
2. resolver/download de checkpoints en el container;
3. instanciar `DirectLTX25Backend` con el `model_root` real;
4. registrar `LTXVideoTaskRunner` mediante `TaskRunnerRegistry.phase8(...)`;
5. ampliar readiness para CUDA, modelos y pipeline;
6. validar una inferencia real y después varias inferencias secuenciales sin crecimiento anómalo de
   VRAM.

## Cierre de 8.2

La validación de 2026-09-04 confirmó:

- Ruff aprobado en CI;
- suite pytest completa aprobada sin requerir Torch/LTX en el entorno normal;
- cálculo de frames probado contra la rejilla temporal `8k + 1`;
- contrato `video.ltx25.generate` validado por tests;
- reutilización de una sola instancia de pipeline en generaciones consecutivas;
- encoding del artefacto con `audio=None`;
- carga lazy de dependencias Torch/LTX;
- `gpu/runtime.py` permanece sin activar todavía el task GPU real.

La inferencia real sobre RTX 5090 no se repite dentro de esta subfase: el adaptador reutiliza la
baseline ya medida en Fase 7 y su primera ejecución end-to-end pertenece al gate de Fase 8.3, cuando
exista la imagen de worker GPU de producción.

**Fase 8.2 cerrada. El siguiente paso es Fase 8.3: worker GPU de producción con LTX-2.5 residente.**
