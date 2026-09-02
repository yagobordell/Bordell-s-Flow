# Continuar la Fase 7

El checkpoint de Salad del 2 de septiembre de 2026 quedó en la versión 10, detenido, sin cambios
pendientes y con la imagen de benchmark v8 preparada.

Desde un ordenador nuevo, empieza por:

```powershell
git clone https://github.com/yagobordell/ai-video-factory.git
Set-Location .\ai-video-factory
.\scripts\resume_phase7_benchmark.ps1 -Action Status
```

La instalación, recuperación de credenciales, ejecución completa, descarga, parada y recuperación
del keyframe están documentadas en
[`docs/phase7-resume-new-pc.md`](docs/phase7-resume-new-pc.md).
