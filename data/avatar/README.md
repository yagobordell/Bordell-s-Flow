# Avatares locales

Coloca aquí tus imágenes de avatar en formato **PNG**, por ejemplo `monje.png`.
Al ejecutar el pipeline B podrás elegir una de estas imágenes después del guion.
Los PNG son archivos locales ignorados por Git: no subas aquí imágenes personales.

El runner valida que la imagen seleccionada es realmente PNG, copia sus bytes a
`data/output/<guion>/avatar.png` y registra su nombre, origen, dimensiones y
SHA-256 en `run_report.json` y `visual_plan.json`. Así la imagen elegida queda
fijada para la ejecución aunque más tarde cambies el archivo original.

La imagen se asocia a los beats cuyo `visual_type` sea `avatar` o
`avatar_media` mediante el campo `avatar` del plan visual. Los prompts y
las respuestas originales de B2 no se modifican. El vídeo/lip-sync del avatar
se implementará cuando se conecten las fases audiovisuales al plan B2.
