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
  -> referencias visuales y keyframes
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
- Windows PowerShell para el runner end-to-end
- Node.js 22+ y npm
- FFmpeg + ffprobe
- Credenciales para OpenAI, SaladCloud, Cloudflare R2 y Postgres/Supabase
- Hugging Face cuando lo requieran los modelos configurados
- Docker solo para construir, preparar o probar workers; no para una ejecución normal con Salad ya preparado

## Instalación

```powershell
python -m pip install -e ".[dev]"

Copy-Item .env.example .env

Push-Location .\remotion
npm.cmd ci
Pop-Location
```

Completa `.env` con las credenciales y configuración necesarias. Los secretos y los artefactos
generados no deben versionarse.

## Ejecución

Coloca el guion en `data/input/script.txt` y ejecuta:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\pipeline\run_video_factory.ps1 `
    -Input .\data\input\script.txt `
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

## Desarrollo

```powershell
python -m pytest
python -m ruff check .
```

## Documentación

- [Arquitectura](docs/architecture/overview.md)
- [Runner de producción](docs/operations/production-runner.md)
- [Configuración de Salad](deploy/salad/services.json)
- [Índice de documentación](docs/README.md)
