# AI Video Factory

Pipeline modular para generar vídeos cinematográficos 16:9 a partir de un guion terminado. La
orquestación combina planificación estructurada, generación de audio e imagen, vídeo en GPU remota,
upscale y composición final reproducible.

## Estado actual

El único flujo de **bots de planificación** es B1.1 → B1.2 → B2. Procesa el
guion sin modificarlo, genera beats por bloque y selecciona estrategias visuales
sin convertirlas a los contratos anteriores de escenas/shots.

El anterior runner de producción de extremo a extremo y los bots que lo alimentaban
se han retirado. **Todavía no existe un recorrido automatizado B2 → vídeo final**:
las fases de narración, Qwen, LTX, Real-ESRGAN y Remotion conservan herramientas
y clientes independientes, pero requieren la migración explícita a los nuevos
beat IDs y tipos visuales antes de poder componerse en una única ejecución.
Consulta [estado de la integración](docs/operations/production-runner.md).

Los servicios GPU de Salad, la cola autoritativa en Postgres, R2, el Capacity
Controller y los mecanismos de reintento/recuperación permanecen disponibles.
Ideogram sigue siendo opcional; Qwen es el generador de imágenes utilizado
en las herramientas de imagen.

## Requisitos

- Python 3.12+
- uv 0.12.18
- Windows PowerShell para los scripts operativos de Salad y los runners GPU controlados
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

## Ejecución de B1.1 → B1.2 → B2

Guarda los guiones UTF-8 directamente en `data/input/`, por ejemplo
`data/input/historia_roma.txt`. Los archivos locales están ignorados por Git;
el runner crea la carpeta al listar o seleccionar guiones si no existe.

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --list-scripts
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py --script historia_roma.txt
```

Sin `--script`, el runner ofrece un menú en una terminal interactiva.
Cada guion escribe en `data/output/<nombre-del-guion>/`: input/output de
B1.1, output por bloque y merge de B1.2/B2, `visual_plan.json` y
`run_report.json` con costes, tiempos y metadatos. La consola imprime
una línea de tiempo y coste inmediatamente después de terminar cada bot.

**Límite actual:** esto genera un plan visual, **no** un vídeo final.
Los wrappers GPU independientes y los servicios de Salad siguen en el
repositorio; no están conectados automáticamente a este plan.
Consulta [la guía del pipeline B](docs/components/b-pipeline.md).

## Desarrollo

```powershell
uv lock --check
uv run --locked --extra dev python -m pytest
uv run --locked --extra dev ruff check .
```

## Documentación

- [Arquitectura](docs/architecture/overview.md)
- [Auditoría de retirada de bots y preservación de Salad](docs/architecture/planning-retirement-audit.md)
- [Estado de la integración del vídeo](docs/operations/production-runner.md)
- [Configuración de Salad](deploy/salad/services.json)
- [Índice de documentación](docs/README.md)
