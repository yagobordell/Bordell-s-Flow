# AI Video Factory

Pipeline modular para generar vídeos cinematográficos 16:9 a partir de un guion terminado. La
orquestación combina planificación estructurada, generación de audio e imagen, vídeo en GPU remota,
upscale y composición final reproducible.

## Estado actual

El pipeline de producción está implementado hasta **Fase 9 — Compositor**. La siguiente etapa es
**Fase 10 — Agentes de verificación**.

Flujo principal:

```text
guion
  -> planificación narrativa y shots
  -> narración + alineación temporal
  -> Qwen-Image-2.1: referencias y keyframes 1280x736 sin recorte
  -> LTX-2.5: vídeo 1280x720 @ 24 fps
  -> Real-ESRGAN x2: 2560x1440 @ 24 fps
  -> Remotion + FFmpeg
  -> FinalVideo
```

Ruta de producción por defecto:

- OpenAI: planificación estructurada.
- Breeze TTS 2: narración principal.
- Fish Speech S2 Pro: fallback de narración.
- Whisper Large V3 Turbo: alineación/transcripción.
- Qwen-Image-2.1: generador de imágenes activo.
- LTX-2.5: generación de vídeo.
- Real-ESRGAN x2: upscale.
- Remotion + FFmpeg: composición y mux final.

Ideogram permanece implementado para usos futuros, pero no forma parte del flujo normal.

## Requisitos

- Python 3.12+
- uv 0.12.18
- Windows PowerShell para el runner end-to-end
- Node.js 22+ y npm
- FFmpeg + ffprobe
- Credenciales para OpenAI, SaladCloud, Cloudflare R2 y Postgres/Supabase
- Hugging Face cuando lo requieran los modelos configurados
- Docker solo para construir, preparar o probar workers; no para una ejecución normal con Salad ya preparado

## Instalación

```powershell
python -m pip install "uv==0.12.18"
uv sync --locked --extra dev
.\.venv\Scripts\Activate.ps1

Copy-Item .env.example .env

Push-Location .\remotion
npm.cmd ci
Pop-Location
```

Completa `.env` con las credenciales y configuración necesarias. Los secretos y los artefactos
generados no deben versionarse. Python se instala desde `uv.lock`; cualquier cambio de dependencias
debe actualizar `pyproject.toml` y `uv.lock` conjuntamente.

## Ejecución

Los ejemplos versionados viven en `examples/input/`. Guarda tantos guiones propios como
necesites en `data/input/scripts/`, cada uno con su nombre, por ejemplo
`historia_roma.txt` y `documental_japon.txt`. Sus contenidos no se versionan.
El runner de producción actual acepta la ruta del guion que elijas:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\pipeline\run_video_factory.ps1 `
    -Input .\data\input\scripts\historia_roma.txt `
    -NonInteractive
```

El runner realiza preflight, cache/resume, planificación por dependencias, gestión de workers Salad,
generación multimedia, composición final y cleanup de recursos.

Salida principal:

```text
data/output/phase9/final_video.mp4
```

Métricas y diagnóstico:

```text
data/output/preflight_report.json
data/output/production_metrics.json
data/output/video_factory_metrics.json
```

## Nuevo pipeline B1.1 → B1.2 → B2 (ejecución independiente)

Los nuevos bots de planificación usan GPT-6 Luna con razonamiento medium. Para elegir
interactivamente uno de los guiones de `data/input/scripts/` y ejecutar solo estos
tres bots:

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py
```

Para elegirlo sin menú, usa `--script historia_roma.txt`; para consultar los
disponibles, usa `--list-scripts`. Cada nueva ejecución borra exclusivamente la
salida anterior de ese guion. Los resultados se guardan en
`data/output/<nombre-del-guion>/`, con carpetas `B1.1/`, `B1.2/` y
`B2/` que conservan el `input.json` y `output.json` de cada llamada (por bloque
en B1.2/B2). Tanto B1.2 como B2 incluyen además un `merged_output.json` con
todos los bloques en su orden original. Al terminar, se muestran los costes
estimados en USD y los tiempos de cada bot y del run: se guardan en
`api_costs.json` y `timings.json`. Este runner todavía no sustituye al flujo
de producción de las fases posteriores; consulta
[la guía del pipeline B](docs/components/b-pipeline.md).

## Desarrollo

```powershell
uv lock --check
uv run --locked --extra dev python -m pytest
uv run --locked --extra dev ruff check .
```

## Documentación

- [Arquitectura](docs/architecture/overview.md)
- [Runner de producción](docs/operations/production-runner.md)
- [Configuración de Salad](deploy/salad/services.json)
- [Índice de documentación](docs/README.md)
