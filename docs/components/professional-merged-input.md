# Importar un B2 profesional fusionado desde un TXT

El lanzador normal `run.ps1` puede arrancar la producción profesional
**después de los bots B** si recibe un `.txt` con el contenido JSON del
`B2/merged_output.json` generado por el pipeline. No requiere un nuevo
comando ni banderas especiales: la detección es automática, antes de llamar
a los modelos de planificación.

## Uso

Guardar el JSON B2 completo (sin bloques Markdown) en
`data/enzo/video04.txt` y ejecutar:

```powershell
.\run.ps1 video04 enzo
```

La primera ejecución organiza el archivo en `data/enzo/video04/video04.txt`.
El sistema conserva los bytes originales, el hash de entrada y una copia
normalizada del plan en `outputs/B2/merged_output.json`. En el espacio
interno también registra `source_narration.txt`, concatenando **sin
modificaciones** los `text` de los beats. Los informes indican
`input_kind=professional_merged_b2`, `merged_b2_sha256` y
`narration_sha256`. Las llamadas a B1.1, B1.2, B2 y B2.1 tienen
`expected_calls=0` y coste cero en este run.

El recorrido comienza en V1 y continúa por V2/V3/V4, Gemini TTS, STT,
generación de imágenes, preview y vídeo LTX/B2.2 siguiendo las mismas
reglas operativas, configuraciones de avatar, portada y QR que el modo
profesional habitual. La voz se puede recuperar con `--resume-audio` o
regenerar con `--regenerate-audio`. Las opciones
`--images-only` y `--generate-video` también funcionan
con los checkpoints resultantes.

## Contrato aceptado

- Objeto JSON completo, `schema_version: "b-pipeline-merged-v1"`,
  `pipeline_stage: "B2"`, `visual_style: "profesional"`.
- `narrative_core` con pregunta central y respuesta final, bloques
  completos consecutivos desde 1 y beats `1A`, `1B`, `2A`, etc.
- Metadatos congelados de cada bloque, texto narrado por beat, tipo,
  `visual_type`, `prompt`, `video_prompt`, `audio` y `reference`
  conformes con B2 profesional.
- Si hay `cta_group_id` y `cta_reference_sources`, deben ser completos,
  ordenados, contiguos, validados con el contrato CTA profesional y con
  la portada ebook configurada o suministrada mediante `--ebook-cover`.

El importador valida el contrato Pydantic, las identidades de beats,
la continuidad de grids y los límites de rachas con audio de avatar
antes de comenzar V1. Si un TXT parece un merged B pero está corrupto,
la ejecución se detiene: **no** se envía accidentalmente el JSON a B1.1
como guion en texto libre.

## Alcance y seguridad

Un merged de B2 no incluye necesariamente el parche B2.1, que en un
run normal vive en otro artefacto. En este modo el B2 importado se
considera **el plan visual definitivo**, sin pasar por B2.1.

La continuidad de imágenes puede requerir en el flujo normal una nueva
corrección por B2. En un run importado esto se bloquea expresamente:
un error de continuidad produce un diagnóstico y obliga a corregir el
TXT e iniciar un nuevo run. Nunca se llama a un bot B por sorpresa ni
se modifica el contenido fuente.

Los guiones TXT normales y el modo Casero no cambian. Los comandos
`--stop` sobre etapas B no tienen sentido para este tipo de entrada
y se rechazan. Tampoco se puede forzar `--style casero` sobre un B2
importado profesional.
