# Fase 7.1 — Benchmark reproducible de LTX-2.5

La Fase 7 no debe fijar todavía una GPU de Salad ni una cuantización. La primera entrega captura
evidencia comparable de tiempo de generación y pico de VRAM sobre hardware real.

## Qué implementa

`scripts/run_phase7_benchmark.py` ejecuta un comando de LTX-2.5 sin pasar por un shell:

- separa warmups de runs medidos;
- identifica todas las GPU visibles mediante `nvidia-smi`;
- muestrea memoria usada mientras vive el proceso;
- exige que cada run produzca un archivo no vacío;
- calcula tamaño y SHA-256 del output;
- persiste media, mediana, mínimo, máximo, throughput y pico de VRAM por GPU;
- redacta tokens y claves comunes antes de guardar el comando en el informe.

Cada warmup y cada run medido arranca un proceso nuevo. El tiempo y el throughput son end-to-end:
incluyen carga de modelos, inicialización, inferencia y encode. Esto mide el coste de recuperación de
un worker interrumpible. Cuando exista el worker persistente, se añadirá una medición separada con
el modelo ya residente para estimar throughput sostenido.

El script no instala LTX-2.5 ni descarga pesos. Debe ejecutarse dentro de un entorno GPU donde el
repositorio oficial de [LTX-2](https://github.com/Lightricks/LTX-2) y los checkpoints estén ya
disponibles. El ref por defecto queda registrado como
`Lightricks/LTX-2@a95ab856bf29407b6b066ede0abe1846050db56c` para que el primer benchmark sea
reproducible.

## Ejemplo

El comando real se pasa después de `--` y debe contener exactamente un placeholder literal
`{output}`. Este ejemplo usa el pipeline distilled con el primer keyframe como condicionamiento:

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
  --report data/output/phase7/l40s-distilled-fp8-cpu.json \
  -- \
  python -m ltx_pipelines.distilled \
  --transformer-path models/ltx-2.5/diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors \
  --text-encoder-path models/ltx-2.5/text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors \
  --video-vae-path models/ltx-2.5/vae/ltx-2.5-video-vae-bf16.safetensors \
  --audio-vae-path models/ltx-2.5/vae/ltx-2.5-audio-vae-bf16.safetensors \
  --spatial-upsampler-path models/ltx-2.5/latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors \
  --prompt "A cinematic historical shot with deliberate subject motion and a slow camera push." \
  --image data/output/phase6/storyboard_keyframes/shot_001.png 0 1.0 \
  --width 768 \
  --height 1280 \
  --num-frames 121 \
  --frame-rate 24 \
  --seed 42 \
  --quantization fp8-cast \
  --offload cpu \
  --output-path '{output}'
```

Los campos declarados antes de `--` describen el caso. El comando posterior es la fuente auditable
de cómo se ejecutó. Ambos deben representar la misma configuración.

## Matriz inicial

Mantén constantes prompt, keyframe, resolución, frames, FPS y seed. Compara como mínimo:

| Caso | Quantization | Offload | Objetivo |
|---|---|---|---|
| `distilled-bf16-none` | `bf16` | `none` | Calidad y velocidad base si cabe en VRAM |
| `distilled-fp8-none` | `fp8-cast` | `none` | Ahorro de VRAM sin transferencia a CPU |
| `distilled-fp8-cpu` | `fp8-cast` | `cpu` | Perfil de menor VRAM con penalización de tiempo |

No compares configuraciones con outputs narrativamente distintos. El benchmark decide capacidad y
coste; la revisión visual del clip sigue siendo necesaria antes de elegir el perfil final.

## Artefacto

Cada caso genera un `LTXBenchmarkReport` JSON en `data/output/phase7/`. El MP4 temporal queda en
`data/tmp/phase7/` salvo que se indique otra ruta.

El pico de VRAM representa la memoria total usada que reporta `nvidia-smi` para cada GPU visible,
no únicamente memoria atribuida al proceso. Ejecuta los casos en nodos sin otra carga para obtener
comparaciones válidas. Los warmups pueden preparar cachés del host, pero no mantienen los pesos en
VRAM porque cada invocación es un proceso independiente.

La fase continúa después del benchmark con:

1. selección provisional de GPU y cuantización basada en los informes reales;
2. worker HTTP stateless compatible con Salad Job Queue;
3. inputs y outputs externos en Cloudflare R2;
4. estado de jobs idempotente en Supabase/Postgres;
5. pruebas de interrupción, retry y reconciliación.
