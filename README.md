# AI Video Factory

Pipeline educativo para generar vídeos cortos verticales a partir de un tema.

Objetivos principales:

- Aprender orquestación de agentes y workflows.
- Usar OpenAI para guion y dirección visual.
- Integrar TTS/STT.
- Ejecutar modelos open-source en GPU remota.
- Generar imágenes y clips de vídeo.
- Montar el resultado final con FFmpeg.

## Estado

Actualmente estamos en la **Fase 0 — Cimientos del proyecto**.

## Roadmap

1. **Fase 0 — Base del proyecto**
   - Estructura Python.
   - Configuración mediante variables de entorno.
   - Contratos Pydantic.
   - Tests.
   - Git.

2. **Fase 1 — Guionista y Director**
   - Entrada: un tema.
   - Generación del guion.
   - División en escenas.
   - Prompts visuales estructurados.

3. **Fase 2 — Audio y sincronización**
   - TTS por escena.
   - Medición de duración.
   - STT/alignment para subtítulos.

4. **Fase 3 — Generación de imágenes**
   - Abstracción de proveedores.
   - GPU remota.
   - FLUX/SDXL u otros modelos.

5. **Fase 4 — Image-to-Video**
   - Animación de imágenes.
   - Wan/LTX u otros modelos.

6. **Fase 5 — Montaje**
   - FFmpeg.
   - Audio, clips, subtítulos y música.

7. **Fase 6 — QA y regeneración selectiva**
   - Validaciones.
   - Reintentos.
   - Regeneración por escena.

## Requisitos

- Python 3.12+
- Git

Para las fases posteriores:

- Cuenta/API de OpenAI.
- FFmpeg.
- Acceso opcional a GPU remota.

## Instalación local

Con `uv`:

```bash
uv sync --dev
```

O con `pip`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Copia las variables de entorno:

```bash
cp .env.example .env
```

Ejecuta los tests:

```bash
pytest
```

## Primer flujo previsto

```text
Topic
  ↓
ScriptWriterAgent
  ↓
Script
  ↓
DirectorAgent
  ↓
VideoPlan
```

El objeto `VideoPlan` será el contrato central del pipeline.
