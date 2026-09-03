# Fase 7 — acta de cierre

Estado: **cerrada**. La validación terminó el **3 de septiembre de 2026 UTC** y el Container Group
de benchmark quedó detenido sin instancias. Esta ficha conserva la evidencia suficiente para
continuar con la Fase 8 sin repetir una ejecución GPU.

## Decisión provisional

| Campo | Selección |
|---|---|
| Hardware | NVIDIA GeForce RTX 5090, 32,607 MiB detectados |
| Pipeline | LTX-2.5 distilled |
| LTX source | `Lightricks/LTX-2@a95ab856bf29407b6b066ede0abe1846050db56c` |
| Cuantización | `fp8-cast` |
| Offload | CPU |
| Workload | 768×1280, 121 frames, 24 FPS, seed 42 |
| Repeticiones | 1 warmup + 3 medidas |

Esta es la línea base para implementar la Fase 8, no un bloqueo permanente del proveedor o la
GPU. Una comparación multi-hardware posterior puede optimizar coste o rendimiento sin cambiar los
contratos de Queue, Postgres y R2.

## Resultado del benchmark

| Métrica | Resultado |
|---|---:|
| Duración media | 194.9337 s |
| Mediana | 195.1352 s |
| Mínimo | 185.2017 s |
| Máximo | 204.4642 s |
| Throughput medio end-to-end | 0.6207 FPS |
| Pico de VRAM | 24,513 MiB |

Las tres muestras produjeron MP4 no vacíos. El último artefacto descargado contiene vídeo H.264 de
768×1280 a 24 FPS, 121 frames y 5.0417 s, además de una pista de audio AAC. La revisión por frames
confirmó contenido visible, coherente con el keyframe histórico y con movimiento entre el inicio y
el final.

## Inventario de artefactos

Directorio local canónico: `data/output/phase7/benchmarks/rtx5090-cloud/`. Está ignorado por Git para
no versionar binarios y logs generados.

| Archivo | Bytes | SHA-256 |
|---|---:|---|
| `status.json` | 166 | `10bb637e6562a1a9fe4e70bb00f498020da9151e50dfab1434d1ec61d823d780` |
| `benchmark.log` | 14,785 | `2a52777e5bdf391b69e3883add4663b14206d6762f71d76e0bd5fc7de8c62059` |
| `matrix.json` | 2,225 | `8aceccde902f9f4aa2248d2460a981900c397751ef00bc6c5966dd633f2721ca` |
| `distilled-fp8-cpu.json` | 2,733 | `ced75fd77a39e4400c2dd951b62b31af5c553ff489326909b8e8b73f05b0a6c2` |
| `distilled-fp8-cpu.mp4` | 3,321,302 | `6121925f18ee134278fc2fb1069b334d0779aaf6a3d1d763cafbcfa6cce62b0c` |

`status.json` registra `succeeded` a las `2026-09-03T22:39:23.566706+00:00`. La matriz y el informe
individual contienen las mismas dimensiones, perfil, dispositivos y agregados. El SHA-256 del MP4
coincide con la tercera muestra y con `Get-FileHash` después de la descarga.

## Evidencia de infraestructura

### Smoke inicial

| Campo | Valor |
|---|---|
| Queue | `ai-video-factory-jobs` |
| Container Group | `ai-video-factory-worker` |
| Application job | `phase7-smoke-a1d3883364d8` |
| Salad job | `5e5e0230-bd62-4105-819e-df004a4d1dcf` |
| Estado / intentos | `succeeded` / `1` |
| Input/output SHA-256 | `0b7da0548cee3474b5ae86ed25eaaff071c7da52d3fc9d07977038a1b600d1a9` |
| Output R2 | `jobs/phase7-smoke-a1d3883364d8/output.txt` |

### Replay idempotente

| Campo | Valor |
|---|---|
| Salad replay job | `8e3a92fa-19fe-49f8-a443-04b5c1369a9b` |
| Queue / worker | `succeeded` / `succeeded` |
| `replayed` / intentos | `true` / `1` |
| Output | misma clave y mismo SHA-256 que el smoke |
| Imagen worker | `docker.io/yagobordell/ai-video-factory@sha256:82c93a035dbd25f1fc11e80df8b559f18f3f6cf7edf3b4b9d7332f440cb352cc` |

El replay no incrementó el contador de intentos ni creó un artefacto distinto. El grupo worker volvió
a cero réplicas.

### Benchmark cloud

| Campo | Valor |
|---|---|
| Container Group | `ai-video-factory-bench-rtx5090` |
| Versión | `10` |
| Imagen | `docker.io/yagobordell/ai-video-factory-benchmark:phase7-ltx25-torch211-cu128-natten0216-v8-salad` |
| Runtime validado | Torch `2.11.0+cu128`, CUDA `12.8`, NATTEN `0.21.6` |
| Estado final | `stopped`, 0 instancias |

## Gates de cierre

- [x] Supabase/Postgres y Cloudflare R2 accesibles desde el worker.
- [x] Smoke Queue → worker → Postgres/R2 → Queue completado.
- [x] Imagen de producción observada por digest.
- [x] Replay idempotente con un único intento y artefacto idéntico.
- [x] Benchmark LTX-2.5 ejecutado en GPU real con warmup y tres medidas.
- [x] JSON, log y MP4 descargados, hasheados y validados.
- [x] Vídeo revisado técnica y visualmente.
- [x] Container Groups detenidos/escalados a cero al finalizar.
- [x] Secretos excluidos de Git y de esta evidencia.

## Próximo paso

Abrir la Fase 8 y registrar la tarea LTX-2.5 de producción en el worker idempotente existente. El
resultado debe subirse a R2 y devolverse como metadata; los bytes de vídeo no deben atravesar el
orquestador.
