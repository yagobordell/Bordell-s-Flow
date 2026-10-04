# Herramientas

El punto de entrada de producción es `run.ps1`, que delega en
`scripts/pipeline/run_managed.py` y en
`scripts/pipeline/run_b_pipeline.py`.

- `pipeline/`: motor de B-pipeline, lanzador administrado y comprobación de disponibilidad de R2; no hay ejecutores independientes de las fases retiradas.
- `salad/`: ciclo de vida de los servicios, Postgres y Capacity Controller.
- `smoke/`: pruebas controladas de inferencia, sin integrarse en otra producción.
- `diagnostics/`: inspección de colas, manifiestos y recuperación.
- `youtube/` y `security/`: publicación y controles de credenciales.

El historial de experimentos y las versiones anteriores del flujo se consultan en Git.
