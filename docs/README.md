# Documentación del flujo actual

## Producción

- [Arquitectura](architecture/overview.md)
- [Flujo B1.1 → B1.2 → B2 y producción](components/b-pipeline.md)
- [Voz profesional](components/professional-voice.md)
- [Imágenes profesionales](components/image-upscaler.md)
- [Casero: voz](components/casero-voice-sources.md)
- [Casero: CTA](components/casero-ebook-cta.md)
- [Publicación en YouTube](integrations/youtube.md)

## Infraestructura y operaciones

- [Núcleo compartido de inferencia](architecture/inference-worker-core.md)
- [Despliegue de Salad](operations/salad-stack-deployment.md)
- [Capacity Controller](operations/salad-capacity-controller.md)
- [Validación del despliegue](operations/deployment-validation.md)
- [LTX 2.5](components/ltx25-worker.md)
- [LTX A2V](components/ltx25-a2v.md)
- [Generación directa de vídeos verticales 9:16](components/standalone-vertical-a2v.md)
- [Servicio de upscale](operations/image-upscaler-salad.md)

Los valores mutables de despliegue pertenecen a
`deploy/salad/services.json`. La documentación histórica, los informes
de experimentos y las instrucciones de runners retirados se conservan
en el historial de Git.
