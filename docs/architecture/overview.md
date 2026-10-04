# Arquitectura actual

El flujo oficial es el lanzador administrado `run.ps1` →
`scripts/pipeline/run_managed.py` →
`scripts/pipeline/run_b_pipeline.py`. B1.1 bloquea fragmentos del guion
sin alterar sus palabras, B1.2 los divide en beats y B2 asigna las
estrategias visuales. Cada avatar conserva su estilo de producción.

Tras B2, el modo profesional aplica V1/V2/V3/V4 y Gemini para la voz.
Casero utiliza su propio flujo Fish y CTA. Ambos pueden producir
imágenes, previsualización, clips de vídeo con LTX, composición final y
publicación administrada; las solicitudes de pago cuentan con estados
verificables para reanudación.

La generación de imágenes usa GPT Image con calidad baja por defecto,
calidad media para prompts con `(For context...)` y para todas las rejillas.
Los paneles extraídos de una rejilla nunca se regeneran por separado
con GPT. El acabado profesional amplía fotografías estáticas, no las
imágenes de vídeo extraídas de rejillas.

## Recursos y control

Postgres `gpu.jobs` es la cola autoritativa. R2 conserva las entradas
y salidas verificadas de inferencia. Salad alberga los servicios GPU y el
Capacity Controller ajusta su capacidad a la demanda. Estos servicios,
sus workers y sus pruebas de operación forman parte de la plataforma
actual, aunque las antiguas herramientas autónomas de fases
escenas/shots no sean un segundo orquestador de producción.

## Comandos y documentación

El único lanzador público es `run.ps1`. El motor Python acepta rutas
explícitas para su invocación desde el lanzador administrado.
Consulta [B-pipeline](../components/b-pipeline.md),
[voz profesional](../components/professional-voice.md),
[imágenes profesionales](../components/image-upscaler.md) y
[operación de Salad](../operations/salad-stack-deployment.md).
