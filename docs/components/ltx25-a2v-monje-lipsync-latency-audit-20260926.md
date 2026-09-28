# Auditoría y plan: LTX-2.5 A2V, labios del monje y latencia extremo a extremo

Fecha: 2026-09-26. Rama independiente creada desde origin/main d956cffe8b011c7673a2959c0fc9bcd020a7af3b. Documento de investigación; NO certifica calidad de labios ni un benchmark nuevo. No modifica inferencia, Docker, Salad ni producción.

## Objetivo y límites

Generar un MP4 del monje de cinco segundos con articulación visible y fonéticamente sincronizada, sin deformación ni pérdida de identidad, y minimizar el tiempo completo hasta recuperar el MP4 y confirmar el grupo Salad detenido. La aceptación humana del vídeo es obligatoria; ffprobe, hashes, audio correcto y CI verde no sustituyen esa revisión. Sin GPU de pago, deploy ni merge sin aprobación explícita. No reutilizar experimentos ni ramas de los PR #231/#232; aprovechar exclusivamente las mejoras operativas ya fusionadas en #234; no interferir en #230.

## Insumos y evidencia disponible

- Imagen declarada: data/input/avatar/monje.png, SHA-256 aaaefa0acd0dbf25b7526ccb349fb6ad4fd7a8511b9eadec66107244bd7bbe64.
- WAV declarado: data/input/avatar/monje.wav, SHA-256 18d070d56289a7abb83abe1394bcbcc29d7cab09cf464220c38ac7297bc94553, 5.0 s, 24 kHz mono.
- Semilla 4242; 1280x720 a 24 fps, cuadrícula 121 frames.
- Última solicitud ltx-a2v-monje-verified-eager-20260926-153042-132d94b1e0a5: prompt genérico "An elderly monk speaking calmly to the camera.", perfil reference distilled FP8_CAST/CPU offload/eager SDPA, audio congelado en ambos stages, Stage 1 ocho pasos Euler ancestral y fuerza imagen 0.7; Stage 2 tres pasos Euler y fuerza 1.0. Audio de condicionamiento estéreo con 1000 muestras de silencio para alcanzar 121 frames.
- Metadatos del worker: 153.973 s inferencia, 17.318 s encode/mux, 172.463 s total worker, pipeline_reused=false, pico de VRAM 17164576256 bytes. Estos datos NO incluyen capacidad, arranque, readiness ni parada.
- Tiempos comunicados del smoke completo: 309.6 s capacidad, 727.6 s hasta ready, 201.4 s fase del trabajo y 22.7 s apagado (aproximadamente 21 minutos); identificar solapes y límites exactos antes de comparar dos ejecuciones.
- Vídeo adjunto de WhatsApp: visualmente ofrece mayor apertura de boca en algunos instantes que el último reference; la comparación offline de audio decodificado mono 24 kHz de ambos MP4 dio muestras idénticas. No existen metadatos acreditados del MP4 de WhatsApp: no se atribuye la diferencia a prompt, seed, perfil ni geometría.
- No consta disponible en esta conversación el documento solicitado Pasted markdown(7).md ni están montados el PNG y WAV de origen: deben recuperarse antes de una prueba reproducible. No inventar información de esas fuentes.

### Prompt de calidad que se conservará literalmente

> A medium close-up talking head of the person in the reference image. The person speaks directly to camera with clear visible lip and jaw articulation precisely synchronized to the supplied speech audio. Lip sync matches every spoken phoneme. Natural blinking and restrained head movement. Static camera.

## Auditoría del código en main

1. src/ai_video_factory/workers/ltx25/reference_a2v.py hereda el A2VidPipelineTwoStage fijado en LTX-2 commit a95ab856bf29407b6b066ede0abe1846050db56c. Usa SimpleDenoiser en ambas etapas, sigmas distilled oficiales, Stage 1 EulerAncestral eta=1/s_noise=1/semilla de ruido seed+10000, Stage 2 Euler; usa el mismo latente de audio congelado/zero noise en ambas etapas. El perfil de referencia no recibe video_guider_params. Por tanto el video_modality_scale=1 registrado en los metadatos NO implica que subir ese parámetro cambie esta ruta: exige variante explícita y test.
2. reference_recipe.py ajusta hacia arriba a una cuadrícula temporal 8k+1 y sólo añade silencio al audio de condicionamiento. La conservación de cola hablada debe mantenerse.
3. a2v.py prepara la imagen solicitada a 1280x720, con padding de borde hasta el lienzo interno 1280x768 y recorte posterior. Stage 1 del perfil reference opera a 640x384. El tamaño real de boca/cara en esa cuadrícula requiere medir el PNG original; no se puede concluir que falten píxeles basándose sólo en tamaño del canvas.
4. La ruta reference usa cuantización FP8_CAST, offload CPU y el decodificador DiffVAE eager SDPA de compatibilidad de RTX 5090; no equivale numéricamente a la referencia BF16 de ComfyUI. No cambiar a BF16, kernels, tiling, sampler ni pasos simultáneamente.
5. La readiness de A2V validate() para archivos compartidos comprueba presencia de archivos; el manifiesto atómico se exige en el camino opcional de assets dev. Revisar si en cold-start la readiness puede adelantarse a la verificación íntegra de pesos y medir el efecto; no confundir este riesgo con una causa demostrada del mal lipsync.
6. scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1 ya ofrece -Prompt, -Seed, -SegmentId, -ExpectedPinnedImage opcional, inicia una réplica, espera dos lecturas ready de una misma instancia y hace Stop en finally. La fase total aún debe medirse con límites inequívocos; -MaxGenerationSeconds sólo mide el tiempo worker. Siempre pasar un output-dir nuevo por experimento, imagen fijada por digest y un SegmentId único. Confirmar estado real después de Stop.
7. Las pruebas automatizadas del script y del worker validan transporte, duración, colas de voz, modelos y audio, pero no aperturas /p/, /b/, /m/ ni la estabilidad de identidad.

## Paridad oficial, hechos versus hipótesis

- Guía oficial LTX-2.5 A2V: audio VAE con tokens congelados durante ambas etapas, vídeo generado alrededor del audio, salida con waveform original; CFG=1 para distilled y decodificación de vídeo por tiles. https://docs.ltx.io/open-source-model/usage-guides/audio-to-video
- Workflow ComfyUI fijado en 63a7f34e641203b63e08897868be10f9548ef0be: plantilla distilled A2V de dos etapas, vídeo a 960x544 por defecto, fuerza de imagen 0.7/1.0, Stage 1 ancestral y Stage 2 Euler, ocho más tres pasos, audio congelado reutilizado; desactivar enhancement antes de una comparación con prompt idéntico. https://github.com/Lightricks/ComfyUI-LTXVideo/blob/63a7f34e641203b63e08897868be10f9548ef0be/example_workflows/2.5/LTX-2.5_A2V_Two_Stage_Distilled.json
- El blog oficial de talking avatars publicado el 26 abril 2026 documenta LTX-2.3, NO la receta LTX-2.5 usada aquí; sus sugerencias modality_scale=3 y audio guider CFG=7 no se pueden trasplantar sin cotejar la API exacta. El upstream fijado no ofrece audio_guider_params en __call__ y crea internamente el guider de audio. https://ltx.io/blog/how-to-build-talking-ai-avatars-from-audio
- El upstream Python fijado usa GuidedDenoiser en su ruta de dos etapas, a diferencia del SimpleDenoiser de nuestra ruta reference; con CFG=1 no se asume automáticamente ni igualdad numérica ni diferencia visual, y debe identificarse el primer latente que diverge si dos rutas con igual input no coinciden. https://github.com/Lightricks/LTX-2/tree/a95ab856bf29407b6b066ede0abe1846050db56c
- Comunidad, evidencia anecdótica: usuarios de https://huggingface.co/Lightricks/LTX-2.5/discussions/44 reportan tanto mejora con audio congelado/CFG1 sin guía extra como dependencia fuerte de la semilla o del material; un comentario defiende cambiar Euler ancestral a Euler, contrario al workflow distilled oficial. No convertir tales afirmaciones en configuración de producción. En Reddit se describe degradación de dientes y boca lejos de primer plano, lo que motiva medir píxeles de boca pero no prueba causalidad en nuestro monje: https://www.reddit.com/r/comfyui/comments/1r09pt3/ltx2_full_si2v_lipsync_video_local_generations/

## Plan experimental y prioridades

P0 (sin GPU, calidad primero): recuperar el PNG/WAV originales y Pasted markdown(7).md; confirmar hashes; localizar metadatos auténticos del vídeo WhatsApp si existen; medir ROI de cara/boca después de todo el preprocesamiento y comparar etapas con upstream fijado. Auditoría de código de semilla, prompt encoder, condicionamiento negativo, noise, audio VAE y orden de latentes. No modificar recetas a ciegas.

P1 (una GPU sólo con aprobación): una ejecución reference aislada con el prompt literal de calidad, imagen/audio/seed 4242, 1280x720/24 fps, digest de Docker inmutable, mismo modelo y perfil eager. Cambiar SOLO el prompt respecto del último smoke. Guardar job JSON, hashes, metadata.json, MP4, versión de grupo, instancia, SHA Git y tiempos fríos. Revisar manualmente fonemas, pausas y cara; no asumir éxito por ffprobe.

P2 (si P1 falla y sujeto a aprobación): aislar una hipótesis por prueba: reencuadre de cara o geometría de Stage 1, paridad de imagen/latente, prompt encoder, guided-versus-simple denoiser, sampler o precisión. Mantener P1 como control inalterable; cualquier perfil nuevo es opt-in. No volver a probar reference-compiled: se midieron 403.733 s inferencia y regresión labial. Guided DISK fue ~530 s inferencia y mostró deformación en la cola: tampoco es baseline aprobado.

P3 (sólo después de aprobar vídeo): instrumentar wall clock desde PowerShell a MP4 verificado y Salad detenido: capacidad/descarga Docker, red, bootstrap y manifiesto de pesos, readiness, Postgres/claim, carga lazy y primera inferencia, denoising/decodificación, encode/mux, R2/retrieval y cleanup. Separar primera inferencia fría de la reutilización real del mismo proceso. Priorizar deduplicación de descargas verificadas y esperas, sin alterar ningún frame. No asumir persistencia de caché en Salad sin prueba.

P4 (optimización numérica bajo puerta visual y de coste): evaluar una variable por vez (offload, tiles de decoder, atención, sampler, precisión, resolución o pasos), registrar calidad y tiempo de ciclo completo. Ningún ahorro de worker justifica menor articulación ni una salida incompleta.

## Preflight y puerta de salida

Antes de cualquier GPU: comprobar estado remoto y cola (grupo detenido, min=0, pending=False), digest OCI revisado igual al de Salad, commit de la imagen, hashes de entradas, env sin exponer secretos, R2/Postgres y fuentes Python locales. Asegurar una sola RTX 5090 y SegmentId nuevo. No lanzar si otra tarea posee el grupo. Always-Stop en success/error/timeout y comprobación posterior de stopped, replicas=0, pending=False sin borrar trabajos ajenos.

Cada experimento requiere diferencias exactas frente a control, todos los tiempos, artefactos y revisión humana. PR siempre draft; no despliegue, merge, borrado de rama ni proclamación de lipsync hasta vídeo aceptado, rendimiento end-to-end comparable y CI completamente verde.
