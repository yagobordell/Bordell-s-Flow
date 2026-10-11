# Casero: subtítulos opcionales y motion graphic manual

## Subtítulos (cada vídeo)

Al iniciar un nuevo vídeo casero, el CLI pregunta si se quieren subtítulos
(salvo en ejecución no interactiva). La preferencia se guarda en el
`run_report.json` del vídeo; no se guarda en el avatar.

- `--subtitles`: subtítulos activados
- `--no-subtitles`: subtítulos desactivados
- En automatización sin ambos argumentos: desactivados por compatibilidad.
- Al invocar `--generate-video` desde una planificación ya preparada,
  vuelve a ofrecer la elección si hay terminal interactiva y el vídeo
  todavía no está completo. Una ejecución completada no cambia de opción.

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
| Duración | **Mínimo 13,5 segundos**, para garantizar que cubre cualquier CTA admitido |

Se comprueba el vídeo mediante FFprobe y se fija su SHA-256 en
`data/<avatar>/meta/cta/motion_source.json`. Se verifica antes de utilizarlo
y su duración se valida definitivamente cuando está disponible la locución.
El mínimo de 13,5 s se comprueba **antes de cualquier TTS, imagen o GPU del CTA**:
el máximo permitido para la primera mitad A2V es 12 s y se añaden 1,5 s.
El ensamblador sigue usando A2V para la primera mitad del CTA y usa el
MP4 seleccionado para la segunda mitad. La duración sobrante se recorta
automáticamente a la necesaria: no se exige que el archivo coincida
exactamente con la voz.

Para uso no interactivo:
`--cta-motion automatic` o `--cta-motion manual` en el primer alta del avatar.

Las bibliotecas CTA existentes sin `motion_source.json` mantienen el modo
automático y no vuelven a pedir la selección. Para cambiar el tipo de motion
de un CTA ya generado hace falta una migración/reconciliación explícita de caché.

## Instrucciones paso a paso para añadir el MP4 manual

1. Inicia el alta del avatar casero con el PNG y TXT ya colocados
   según el layout habitual. Por ejemplo:
   ` .\\run.ps1 video01 enzo --style casero `
2. Elige **motion graphic manual** cuando se muestre la pregunta.
   El programa crea y muestra la ruta
   `data/Enzo/meta/cta/motion_manual.mp4` (sustituye `Enzo` por
   el nombre exacto del avatar). Si no existe, permanece esperando
   que coloques un MP4 válido.
3. Copia tu fichero a esa ruta con el nombre exacto
   `motion_manual.mp4`. Desde otra ventana PowerShell puedes usar:

   ```powershell
   Copy-Item "C:\\Videos\\mi_motion.mp4" "data\\enzo\\meta\\cta\\motion_manual.mp4"
   ```

4. Vuelve al terminal de Video Factory y pulsa **Enter**.
   Se valida con FFprobe (códec, resolución, FPS, ausencia de audio,
   duración) y se registra su SHA-256. Un archivo inválido debe
   corregirse antes de continuar; no se llama a ninguna API mientras
   falta o no supera la validación.
5. El archivo es exclusivamente la **segunda mitad visual del CTA**,
   seguida de 1,5 segundos de cola. No sustituye la primera mitad
   A2V del avatar ni la locución. Por tanto, prepara dentro del propio
   MP4 todos los elementos visuales que quieras mostrar en esa parte
   del CTA (por ejemplo portada del ebook, QR auténtico y URL).
   **No se añadirán automáticamente al motion manual** y el
   audio del fichero no se utilizará.

### Convertir y normalizar un motion con FFmpeg

Si tienes un MOV o un MP4 con sonido/resolución/FPS diferentes,
puedes convertirlo a la especificación (manteniendo el aspecto
y añadiendo bandas si hiciera falta):

```powershell
ffmpeg -i "mi_motion_original.mov" -vf "fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2" -an -c:v libx264 -pix_fmt yuv420p -movflags +faststart "motion_manual.mp4"
```

El vídeo resultante también debe durar por lo menos **13,5 s**.
Si es menor, amplíalo en el editor antes de cargarlo; por ejemplo,
mediante un plano final estático de cierre con el QR visible.
Con FFprobe puedes confirmar las pistas y dimensiones:

```powershell
ffprobe -v error -show_streams -show_format "motion_manual.mp4"
```

Los siguientes guiones del mismo avatar reutilizan la animación ya
configurada sin preguntar de nuevo ni regenerarla. Un fichero distinto
puede sustituir al seleccionado **solo antes de validar el motion y
completar el CTA**; una caché completada rechaza cambios para evitar
mezclar recursos entre vídeos.
