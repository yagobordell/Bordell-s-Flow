# Fase 7 cerrada — continuar con la Fase 8

La Fase 7 terminó correctamente el 3 de septiembre de 2026 UTC. El benchmark LTX-2.5 produjo y
validó sus JSON y MP4 en una RTX 5090; después se detuvo el Container Group de Salad.

No ejecutes `-Action Start` ni reconstruyas la imagen para cerrar esta fase. El siguiente trabajo es
la **Fase 8 — generación de vídeo**, usando como baseline provisional RTX 5090, `fp8-cast` y
offload a CPU.

Consulta:

- [`docs/phase7-closure.md`](docs/phase7-closure.md): evidencia, métricas y hashes de cierre.
- [`docs/phase7-resume-new-pc.md`](docs/phase7-resume-new-pc.md): recuperación del checkout y
  comprobaciones seguras desde otro ordenador.
- [`docs/phase7-deployment.md`](docs/phase7-deployment.md): procedimiento de infraestructura si
  alguna vez se decide repetir la validación.
