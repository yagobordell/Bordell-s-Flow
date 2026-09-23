# Fase 8.1 — planificación temporal de vídeo

Estado: **cerrada y validada con los 8 shots reales del ejemplo de samuráis**.

## Objetivo

La Fase 6 define cómo debe verse el estado inicial de cada shot mediante `StoryboardFrame` y
`StoryboardKeyframe`. La Fase 8.1 añade una decisión diferente: **qué movimiento debe ocurrir durante
ese shot**.

La separación canónica queda así:

```text
Shot + ShotTiming + StoryboardFrame
                  ↓
            VideoPromptBot
                  ↓
              VideoPrompt
```

`VideoPrompt` permanece deliberadamente pequeño:

```text
VideoPrompt = { shot_id, prompt }
```

Los parámetros propios del futuro runtime LTX-2.5 — seed, frames, FPS, resolución, cuantización,
offload o checkpoint — no pertenecen a este contrato. Se resolverán en la Fase 8.2 al construir el
job GPU.

## Frontera con el storyboard

`StoryboardFrame.prompt` describe un **keyframe estático**. No se reutiliza sin más como prompt de
vídeo porque la Fase 6 prohíbe deliberadamente movimiento de cámara, transiciones e instrucciones
temporales.

`VideoPromptBot` recibe:

- el `Shot` canónico;
- su `ShotTiming` real;
- el `StoryboardFrame` del mismo `shot_id`;
- estilo visual y aspect ratio como inputs runtime;
- el `VideoPrompt` anterior únicamente cuando sigue dentro de la misma escena.

El bot decide movimiento del sujeto, entorno y cámara cuando sean útiles, pero debe producir un
solo plano continuo. No puede inventar cortes, transiciones, nuevas entidades, texto visible ni
audio narrativo.

## Continuidad explícita

El workflow procesa shots en orden. Dentro de una escena, el prompt anterior se usa como contexto
para evitar discontinuidades temporales. Al cambiar `scene_id`, ese contexto se reinicia.

La continuidad entre stages sigue dependiendo de contratos persistidos, no de memoria oculta del
provider.

Python valida antes de llamar al modelo:

- shots con IDs consecutivos desde 1;
- orden de escenas no decreciente;
- correspondencia exacta y ordenada entre `Shot`, `ShotTiming` y `StoryboardFrame`;
- duración positiva por shot;
- timeline temporal contigua.

Si una de estas invariantes falla, no se realiza ninguna llamada al provider.

## Ejecución

Con los artefactos reales de fases anteriores disponibles y `OPENAI_API_KEY` configurada:

```bash
python scripts/run_phase8_video_prompts.py
```

Inputs por defecto:

```text
data/output/phase3/shots.json
data/output/phase5/shot_timings.json
data/output/phase6/storyboard_frames.json
```

Salida:

```text
data/output/phase8/video_prompts.json
```

También se pueden sustituir rutas, estilo y aspect ratio:

```bash
python scripts/run_phase8_video_prompts.py \
  --shots data/output/phase3/shots.json \
  --timings data/output/phase5/shot_timings.json \
  --storyboard-frames data/output/phase6/storyboard_frames.json \
  --style "cinematic historical documentary" \
  --aspect-ratio 9:16
```

## Gate de cierre de 8.1

La validación real de 2026-09-04 confirmó:

- Ruff y pytest aprobados en CI;
- ejecución correcta con los 8 shots reales del ejemplo de samuráis;
- exactamente un `VideoPrompt` por shot y IDs `[1, 2, 3, 4, 5, 6, 7, 8]`;
- movimiento coherente con las duraciones reales;
- grounding de los elementos revisados contra `StoryboardFrame`;
- ausencia de mojibake en el JSON UTF-8 persistido;
- ausencia de cortes, montaje, audio narrativo o parámetros específicos de LTX-2.5.

La evidencia histórica de validación se conserva en el historial de Git. Este documento describe el
contrato de prompts que forma parte del pipeline de producción actual.
