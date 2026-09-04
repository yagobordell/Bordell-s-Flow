# Fase 8.1 — resultados de validación real

Fecha de validación: **2026-09-04**.

## Resultado

La subfase 8.1 queda **validada funcional y semánticamente** con los artefactos reales del ejemplo de samuráis.

La ejecución:

```text
python scripts/run_phase8_video_prompts.py
```

produjo correctamente:

```text
data/output/phase8/video_prompts.json
```

con exactamente 8 prompts y los IDs ordenados:

```text
[1, 2, 3, 4, 5, 6, 7, 8]
```

## Duraciones reales revisadas

Las duraciones usadas por el motion planner fueron:

| Shot | Duración |
| ---: | ---: |
| 1 | 3.50 s |
| 2 | 7.38 s |
| 3 | 5.06 s |
| 4 | 2.82 s |
| 5 | 9.68 s |
| 6 | 6.60 s |
| 7 | 7.38 s |
| 8 | 2.58 s |

El shot 5 contiene tres microacciones de entrenamiento — katana, arco y caballo — pero dispone de 9.68 s, por lo que la complejidad se considera compatible con su duración.

## Revisión de grounding

Se revisaron especialmente los shots 3 y 6 para comprobar que el motion planner no hubiese inventado elementos visuales.

- El shot 3 usa dos señores feudales rivales porque ambos ya están presentes de forma explícita en `StoryboardFrame`.
- El shot 6 usa un estandarte caído sin bandera porque ya está presente de forma explícita en `StoryboardFrame`.
- El shot 4 conserva el gesto de servicio y la jerarquía ya definidos por el storyboard.
- El shot 5 anima las tres disciplinas ya presentes en el keyframe: espada, arco y equitación.

No se detectaron nuevas entidades narrativas relevantes introducidas por 8.1.

## Codificación

La salida JSON fue leída explícitamente como UTF-8 y no contiene mojibake real. Los caracteres corruptos vistos previamente mediante `Get-Content` en PowerShell eran un problema de representación del terminal, no del artefacto persistido.

## Calidad temporal

Los ocho prompts cumplen la frontera esperada de 8.1:

- un único plano continuo por shot;
- movimiento de sujeto, entorno o cámara cuando aporta información;
- sin cortes ni montaje;
- sin instrucciones de audio narrativo;
- sin parámetros específicos de LTX-2.5;
- preservación del estado visual inicial definido por el storyboard.

Se observa repetición del recurso de cámara `slow push-in` en varios shots. No bloquea 8.1 porque la planificación temporal es válida y provider-neutral, pero conviene observar este patrón en la primera tanda de clips de LTX-2.5 antes de decidir si el prompt system necesita mayor diversidad de cámara.

## Gate de cierre

- CI: Ruff aprobado.
- CI: pytest aprobado.
- Ejecución real: aprobada.
- Correspondencia 1 prompt por shot: aprobada.
- Orden de IDs: aprobado.
- Grounding contra storyboard: aprobado.
- Coherencia movimiento/duración: aprobada.
- UTF-8 persistido: aprobado.

**Conclusión: Fase 8.1 cerrada. El siguiente trabajo puede comenzar en Fase 8.2: adaptador directo LTX-2.5/PyTorch y contrato de generación GPU.**
