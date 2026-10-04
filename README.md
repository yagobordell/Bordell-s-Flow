# AI Video Factory

Flujo actual para crear vídeos a partir de guiones y avatares: **B1.1 → B1.2 → B2 → voz e imágenes → preview → LTX → composición y publicación**. El lanzador oficial es `run.ps1`, que utiliza `scripts/pipeline/run_managed.py` y el motor `scripts/pipeline/run_b_pipeline.py`. No se utiliza el antiguo árbol `data/input` / `data/avatar` / `data/output` ni el antiguo orquestador por fases.

## Instalación

- Python 3.12+, `uv==0.12.18`, PowerShell y FFmpeg/ffprobe.
- Configura `.env` a partir de `.env.example`, con las credenciales que necesites para el estilo y los servicios seleccionados.
- Para generar vídeo, configura los servicios LTX, la cola Postgres y R2 tal y como se describen en la [documentación de B-pipeline](docs/components/b-pipeline.md) y en [Salad](docs/operations/salad-stack-deployment.md).
- Para publicar en YouTube, instala las dependencias opcionales y configura `YOUTUBE_ENABLED=true`.

```powershell
python -m pip install "uv==0.12.18"
uv sync --locked --extra dev
.\run.ps1 --help
```

Los datos y los secretos de las ejecuciones no deben incorporarse al repositorio. Las dependencias se fijan en `pyproject.toml` y `uv.lock`.

## Estructura actual de trabajo

La interfaz administrada utiliza `data/<avatar>/`. En el primer uso, coloca `Jorge.png` y `test01.txt` directamente en `data/`. El lanzador organiza el avatar en `data/Jorge/`, el guion en `data/Jorge/test01/` y crea `data/Jorge/meta/` para conservar el QR, la voz y el estilo elegidos para ese avatar. En posteriores ejecuciones, coloca los guiones TXT pendientes directamente en `data/Jorge/`.

Cada guion conserva `outputs/`, `media/images`, `media/audio`, `media/preview` y el vídeo final en la carpeta correspondiente. Los checkpoints verificables se conservan fuera del árbol público, en `.video_factory_work/<avatar>/<guion>/`.

```powershell
.\run.ps1 test01 Jorge --style profesional
.\run.ps1 test02 Jorge --no-video
.\run.ps1 test02 Jorge --generate-video
.\run.ps1 test02 Jorge --images-only
.\run.ps1 test02 Jorge --resume-audio
.\run.ps1 test02 Jorge --sync-only
.\run.ps1 test01 Jorge --rerun
```

También existe `--style casero`; la elección se guarda por avatar y las siguientes ejecuciones no pueden modificarla silenciosamente. `--rerun` archiva la ejecución anterior; no lo combines con opciones de reanudación. Para ver todas las opciones, ejecuta `.\run.ps1 --help`.

El modo profesional usa V1/V2/V3/V4, Gemini y el acabado de imágenes profesionales. Las imágenes individuales se generan en calidad `low`, salvo que su propio prompt tenga `(For context...)`, en cuyo caso usan `medium`. Las rejillas usan siempre `medium` y ningún panel se regenera por separado con GPT. Los paneles para vídeo conservan la resolución real permitida por la rejilla. Casero mantiene su propio tratamiento de voz, imágenes y CTA. Consulta [B-pipeline](docs/components/b-pipeline.md), [voz profesional](docs/components/professional-voice.md) y [acabado de imágenes](docs/components/image-upscaler.md).

## Infraestructura y validación

El flujo B utiliza los proveedores externos configurados, los servicios GPU necesarios en Salad, Postgres como cola autoritativa y R2 para los activos de inferencia. Los workers y las pruebas de infraestructura siguen siendo parte de la operación actual; las herramientas autónomas de diagnóstico de GPU no son un segundo flujo de producción.

```powershell
uv run --locked --extra dev ruff check .
uv run --locked --extra dev python -m pytest
```

Consulta el [índice de documentación actual](docs/README.md), la [arquitectura](docs/architecture/overview.md) y la [guía de despliegue](docs/operations/deployment-validation.md). El historial y los documentos de experimentos retirados permanecen disponibles en Git, no en el árbol de trabajo.
