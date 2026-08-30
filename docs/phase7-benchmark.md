# Fase 7.1 — matriz reproducible de LTX-2.5

Estado: **implementada; ejecución real pendiente**. La Fase 7 no se cierra hasta obtener los JSON
de hardware real y registrar la selección provisional de GPU, cuantización y offload.

## Dónde se ejecuta

El benchmark se ejecuta en cada máquina GPU candidata, no en el portátil si este no dispone de la
GPU y VRAM que se quieren medir. Puede ser una instancia temporal de Salad, otro proveedor GPU o
una máquina local NVIDIA. En todos los casos deben mantenerse idénticos:

- commit de LTX-2 y checkpoints;
- prompt, keyframe, resolución, frames, FPS y seed;
- versiones de CUDA/PyTorch;
- warmups y runs medidos;
- ausencia de otros procesos que consuman GPU.

El worker de infraestructura `ai-video-factory:phase7` no contiene LTX ni sus pesos. No debe usarse
para esta matriz. El benchmark necesita un entorno GPU separado con LTX-2.5 instalado.

## Preparar LTX-2.5

En la máquina GPU, instala `git`, Python 3.12, `uv`, Git LFS si lo exige el entorno, CUDA compatible
y los drivers NVIDIA. Después:

```bash
git clone https://github.com/Lightricks/LTX-2.git
cd LTX-2
git checkout a95ab856bf29407b6b066ede0abe1846050db56c
uv sync --extra natten
```

Acepta previamente las condiciones del modelo en Hugging Face y autentica un token de lectura.
La descarga siguiente ronda los 66 GiB:

```bash
hf auth login
hf download Lightricks/LTX-2.5 \
  diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors \
  text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors \
  vae/ltx-2.5-video-vae-bf16.safetensors \
  vae/ltx-2.5-audio-vae-bf16.safetensors \
  latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors \
  --local-dir models/ltx-2.5
```

Instala el proyecto AI Video Factory en el entorno creado por LTX. En PowerShell, desde la carpeta
`LTX-2`, sustituye la ruta por la ubicación real del repositorio:

```powershell
$Factory = "C:\Users\User\Downloads\ai-video-factory"
uv pip install --python ".venv\Scripts\python.exe" -e $Factory
& ".venv\Scripts\python.exe" -c "import ai_video_factory; print('AI Video Factory: OK')"
nvidia-smi
```

En Linux usa `.venv/bin/python` en lugar de `.venv\Scripts\python.exe`.

## Qué mide

`scripts/run_phase7_benchmark_matrix.py`:

- identifica todas las GPU visibles mediante `nvidia-smi`;
- ejecuta un warmup separado y tres procesos medidos por caso;
- muestrea el pico de VRAM mientras vive cada proceso;
- exige un MP4 no vacío y registra tamaño y SHA-256;
- calcula media, mediana, mínimo, máximo y FPS end-to-end;
- escribe un informe por caso y un `matrix.json` por hardware.

Cada proceso incluye carga de pesos, inferencia y encode. Es la medida correcta para un worker
interrumpible que puede arrancar en frío; no representa throughput con un modelo residente.

## Ejecutar la matriz canónica

Desde la carpeta `LTX-2`, con el keyframe copiado a una ruta accesible, ejecuta este bloque en
PowerShell. Las llaves de los tres placeholders deben llegar literalmente al script:

```powershell
$Factory = "C:\Users\User\Downloads\ai-video-factory"
$Python = ".venv\Scripts\python.exe"
$Hardware = "rtx4090" # usa l40s, rtx4090 o rtx5090 según la máquina

& $Python "$Factory\scripts\run_phase7_benchmark_matrix.py" `
  --hardware-label $Hardware `
  --pipeline distilled `
  --prompt "A cinematic historical shot with deliberate subject motion and a slow camera push." `
  --conditioning-image "$Factory\data\output\phase6\storyboard_keyframes\shot_001.png" `
  --seed 42 `
  --width 768 `
  --height 1280 `
  --num-frames 121 `
  --fps 24 `
  --warmup-runs 1 `
  --measured-runs 3 `
  --output-dir "$Factory\data\output\phase7\benchmarks" `
  --temp-dir "$Factory\data\tmp\phase7\benchmarks" `
  -- `
  $Python -m ltx_pipelines.distilled `
  --transformer-path models/ltx-2.5/diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors `
  --text-encoder-path models/ltx-2.5/text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors `
  --video-vae-path models/ltx-2.5/vae/ltx-2.5-video-vae-bf16.safetensors `
  --audio-vae-path models/ltx-2.5/vae/ltx-2.5-audio-vae-bf16.safetensors `
  --spatial-upsampler-path models/ltx-2.5/latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors `
  --prompt "{prompt}" `
  --image "{conditioning_image}" 0 1.0 `
  --width 768 `
  --height 1280 `
  --num-frames 121 `
  --frame-rate 24 `
  --seed "{seed}" `
  "{quantization_args}" `
  "{offload_args}" `
  --output-path "{output}"
```

Los placeholders de prompt, imagen y seed obligan a que todos los casos usen la entrada declarada;
sus hashes quedan en `matrix.json`. Los placeholders `quantization_args` y `offload_args` ocupan un
argumento completo. Para el caso
BF16/none el runner los elimina; para los demás los expande a `--quantization <valor>` y
`--offload <valor>`. Esto evita pasar valores ficticios a la CLI de LTX.

Por defecto se ejecutan estos tres casos:

| Caso | Quantization | Offload | Objetivo |
|---|---|---|---|
| `distilled-bf16-none` | `bf16` | `none` | Base de calidad y velocidad si cabe en VRAM |
| `distilled-fp8-none` | `fp8-cast` | `none` | Reducir VRAM sin transferencias a CPU |
| `distilled-fp8-cpu` | `fp8-cast` | `cpu` | Reducir VRAM aceptando penalización de tiempo |

Se puede añadir o reemplazar la matriz con `--case LABEL:QUANTIZATION:OFFLOAD`. No cambies el
workload entre GPUs. Si `bf16` no cabe, conserva el error/log como evidencia y ejecuta los casos que
sí sean viables; no presentes un caso fallido como una medición.

## Artefactos esperados

Cada máquina genera:

```text
data/output/phase7/benchmarks/<hardware>/
├── distilled-bf16-none.json
├── distilled-fp8-none.json
├── distilled-fp8-cpu.json
└── matrix.json
```

Los MP4 de cada run son temporales y se guardan bajo `data/tmp/phase7/benchmarks/<hardware>/`.
Revísalos visualmente: el caso más rápido no es automáticamente el perfil elegido.

## Unir las matrices

Copia las carpetas de las máquinas GPU al mismo checkout y ejecuta desde la raíz de AI Video
Factory:

```powershell
python scripts/summarize_phase7_benchmarks.py `
  data/output/phase7/benchmarks/l40s/matrix.json `
  data/output/phase7/benchmarks/rtx4090/matrix.json `
  data/output/phase7/benchmarks/rtx5090/matrix.json `
  --output data/output/phase7/benchmarks/comparison.json
```

El resumen rechaza matrices con workloads distintos, registra el SHA-256 de cada matriz fuente y
señala el caso más rápido. La decisión final debe añadir disponibilidad y coste por clip, y confirmar
calidad visual.

## Criterio de cierre de 7.1

- `matrix.json` real de cada hardware candidato viable;
- `comparison.json` generado sin incompatibilidades;
- clips revisados visualmente;
- GPU, cuantización y offload provisionales registrados en README y arquitectura;
- cualquier candidato omitido justificado por disponibilidad o incapacidad demostrada.

Referencias: [instalación oficial de LTX-2](https://github.com/Lightricks/LTX-2/blob/main/packages/ltx-pipelines/docs/installation.md)
y [repositorio oficial](https://github.com/Lightricks/LTX-2).
