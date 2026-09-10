# Fase 8.2 — adaptador directo LTX-2.5

Estado: **cerrada; el adapter quedó validado posteriormente con inferencia GPU real en Fases 8.3 y 8.4**.

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

## Relación con Fases 8.3 y 8.4

8.2 cerró primero el contrato y la implementación del adapter sin activar todavía el runtime GPU de
producción. Fase 8.3 añadió la imagen de worker real, descarga de checkpoints, readiness y registro de
`LTXVideoTaskRunner` mediante `TaskRunnerRegistry.phase8(...)`.

La validación cloud posterior confirmó:

1. construcción correcta de la imagen GPU con el runtime Torch/LTX;
2. descarga y validación de los cinco checkpoints;
3. preparación de `DirectLTX25Backend` con el `model_root` real;
4. registro efectivo de `video.ltx25.generate` en el worker de producción;
5. readiness únicamente tras CUDA, modelos, storage/state y pipeline residente;
6. inferencia real en RTX 5090;
7. varias inferencias secuenciales sobre la misma instancia caliente;
8. replay idempotente de application jobs ya completados;
9. reutilización del mismo adapter en el fanout completo de 8 shots de Fase 8.4.

## Cierre de 8.2

La validación inicial de 2026-09-04 confirmó:

- Ruff aprobado en CI;
- suite pytest completa aprobada sin requerir Torch/LTX en el entorno normal;
- cálculo de frames probado contra la rejilla temporal `8k + 1`;
- contrato `video.ltx25.generate` validado por tests;
- reutilización de una sola instancia de pipeline en generaciones consecutivas;
- encoding del artefacto con `audio=None`;
- carga lazy de dependencias Torch/LTX.

La validación real sobre RTX 5090 se completó posteriormente en Fase 8.3 y quedó reutilizada a escala
de storyboard completo en Fase 8.4. Por tanto ya no existe ningún gate GPU pendiente para esta
subfase.

**Fase 8.2 cerrada y validada dentro del cierre global de Fase 8.**
