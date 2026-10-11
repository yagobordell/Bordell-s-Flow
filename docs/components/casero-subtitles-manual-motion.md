# Casero: subtítulos opcionales y motion graphic manual

## Subtítulos (cada vídeo)

Al iniciar un nuevo vídeo casero, el CLI pregunta si se quieren subtítulos
(salvo en ejecución no interactiva). La preferencia se guarda en el
`run_report.json` del vídeo; no se guarda en el avatar.

- `--subtitles`: subtítulos activados
- `--no-subtitles`: subtítulos desactivados
- En automatización sin ambos argumentos: desactivados por compatibilidad.

Los subtítulos son blancos, pequeños (30px en Full HD), con sombra negra,
centrados abajo, sin fondo y **siempre una sola línea**. Se generan localmente
a partir de los STT de narración existentes; **no hay subtítulos durante el CTA
casero**. Los eventos anteriores se mantienen y los posteriores se desplazan
según la duración real de la inserción del CTA.

El vídeo sin subtitular se conserva en el directorio de trabajo. Si se activan,
se publica el MP4 con subtítulos incrustados y, junto a él, `<guion>.srt`.
No se repite TTS, STT, imágenes ni inferencia LTX por los subtítulos.
La opción se puede especificar también en `--generate-video` si aún no se ha
completado el MP4; un resultado terminado no permite modificar la preferencia
en la misma ejecución.

## Motion graphic CTA (una vez por avatar casero)

Al dar de alta un avatar en modo casero, elegir:

1. **Automático**: generación histórica de textos CTA Motion y animación Python.
2. **Manual**: se utiliza un MP4 propio. No se llama al bot de texto CTA Motion
   ni al renderizador automático del motion graphic.

Copiar el archivo manual exactamente a:

```text
data/<avatar>/meta/cta/motion_manual.mp4
```

Especificaciones **obligatorias**:

| Propiedad | Valor |
| --- | --- |
| Formato | MP4 H.264 |
| Resolución | 1920 × 1080 |
| Fotogramas | 30 FPS constante |
| Pixel format | yuv420p |
| Sonido | Ninguna pista de audio |
| Duración | Como mínimo la mitad de la locución CTA más 1,5 s |

Se comprueba el vídeo mediante FFprobe y se fija su SHA-256 en
`data/<avatar>/meta/cta/motion_source.json`. Se verifica antes de utilizarlo
y su duración se valida definitivamente cuando está disponible la locución.
El ensamblador sigue usando A2V para la primera mitad del CTA y usa el
MP4 seleccionado para la segunda mitad. Una duración excesiva se recorta;
la insuficiente causa un error, nunca una sustitución automática.

Para uso no interactivo:
`--cta-motion automatic` o `--cta-motion manual` en el primer alta del avatar.

Las bibliotecas CTA existentes sin `motion_source.json` mantienen el modo
automático y no vuelven a pedir la selección. Para cambiar el tipo de motion
de un CTA ya generado hace falta una migración/reconciliación explícita de caché.
