# Reanudar la Fase 7 después de formatear el ordenador

Estado guardado el **2 de septiembre de 2026**:

| Campo | Valor |
|---|---|
| Organización Salad | `yagobordellorg` |
| Proyecto Salad | `aivideofactory` |
| Container Group | `ai-video-factory-bench-rtx5090` |
| Versión preparada | `10` |
| Estado | `stopped` |
| Cambio pendiente | `false` |
| Imagen | `docker.io/yagobordell/ai-video-factory-benchmark:phase7-ltx25-torch211-cu128-natten0216-v8-salad` |
| GPU | RTX 5090, clase `851399fb-7329-4195-a042-d6514b28cf33` |
| Recursos | 8 vCPU, 60 GiB RAM, 8 GiB SHM, 128 GiB storage |
| Caso | `distilled-fp8-cpu`, 1 warmup + 3 runs |

La versión 10 está preparada y detenida. **No hay que reconstruir ni volver a parchear la imagen**
para retomar la ejecución. Salad, Docker Hub, Supabase y R2 son servicios remotos y no se borran al
formatear el PC.

## Antes de formatear

Guarda en un gestor de contraseñas, nunca en Git ni en capturas públicas:

- Salad API key;
- token de lectura de Hugging Face con acceso a `Lightricks/LTX-2.5`;
- usuario y PAT de Docker Hub;
- acceso a GitHub y códigos de recuperación de 2FA;
- `POSTGRES_DSN` de Supabase;
- `R2_ENDPOINT_URL`, `R2_BUCKET`, `R2_ACCESS_KEY_ID` y `R2_SECRET_ACCESS_KEY`;
- credenciales de recuperación de las cuentas de Supabase y Cloudflare.

Comprueba en el portal de Salad que el grupo sigue en `stopped`. No borres el Container Group ni
sobrescribas la etiqueta Docker v8. Si tienes otros outputs locales no versionados, cópialos a un
disco externo. El keyframe canónico sí puede recuperarse desde la imagen v8 con el script incluido.

## Preparar un Windows nuevo

Abre **PowerShell como usuario normal** e instala Git:

```powershell
winget install --id Git.Git -e
```

Cierra y abre PowerShell. Clona el repositorio:

```powershell
Set-Location "$HOME\Downloads"
git clone https://github.com/yagobordell/ai-video-factory.git
Set-Location ".\ai-video-factory"
git status
```

Para arrancar y descargar el benchmark cloud solo hacen falta Git, PowerShell, acceso a Internet y
la API key de Salad. Python y Docker no intervienen en esa ejecución remota.

Si más adelante necesitas reconstruir imágenes, instala también Docker Desktop y Python 3.12:

```powershell
winget install --id Docker.DockerDesktop -e
winget install --id Python.Python.3.12 -e
```

Reinicia Windows después de instalar Docker Desktop, abre la aplicación y ejecuta `docker login`.

## Cargar la API key solo en memoria

En la PowerShell desde la que ejecutarás todo:

```powershell
$SecureSaladKey = Read-Host "Salad API key" -AsSecureString
$env:SALAD_API_KEY = (
    [PSCredential]::new("salad", $SecureSaladKey)
).GetNetworkCredential().Password
Set-ExecutionPolicy -Scope Process Bypass
```

La variable desaparece al cerrar esa PowerShell. No uses `setx` y no guardes el valor en el
repositorio.

## Comprobar el checkpoint

```powershell
.\scripts\resume_phase7_benchmark.ps1 -Action Status
```

Debe mostrar como mínimo versión 10, `pending_change=False`, estado `stopped` y la imagen v8 exacta.
El script aborta si detecta otra imagen o una versión anterior.

## Arrancar, monitorizar, descargar y detener

Ejecuta los cuatro pasos en orden. El monitor puede permanecer activo durante horas porque la
descarga de los cinco modelos LTX-2.5 ronda decenas de GiB:

```powershell
.\scripts\resume_phase7_benchmark.ps1 `
    -Action Start `
    -TimeoutMinutes 180

.\scripts\resume_phase7_benchmark.ps1 `
    -Action Monitor `
    -TimeoutMinutes 720

.\scripts\resume_phase7_benchmark.ps1 `
    -Action Download

.\scripts\resume_phase7_benchmark.ps1 `
    -Action Stop
```

`Start` rechaza automáticamente el nodo
`8e9fc285-4a97-145d-b192-faed64f70e29`, asociado repetidamente a `exit 139`, y solicita una nueva
asignación. `Download` guarda y valida los JSON en
`data/output/phase7/benchmarks/rtx5090-cloud/`. Cuando existe, también descarga el MP4 y muestra su
SHA-256. `Stop` espera hasta que el grupo esté detenido y sin instancias.

Si cualquier paso falla, detén el grupo para cortar el consumo:

```powershell
.\scripts\resume_phase7_benchmark.ps1 -Action Stop
```

Después exporta los logs y System Events de Salad antes de volver a arrancar.

## Recuperar el keyframe para una futura imagen

El PNG no se duplica en Git. Está contenido en la imagen v8 publicada. Con Docker Desktop iniciado:

```powershell
.\scripts\resume_phase7_benchmark.ps1 -Action RecoverKeyframe
```

El script crea un contenedor temporal con nombre fijo, copia únicamente `shot_001.png`, elimina ese
contenedor y muestra el SHA-256. No arranca el contenedor ni consume GPU.

## Criterio de cierre pendiente

La infraestructura y el smoke/replay de la Fase 7.2 ya están cerrados. La Fase 7 global se cierra
cuando esta ejecución produzca:

- `matrix.json` válido;
- `distilled-fp8-cpu.json` válido;
- un MP4 no vacío y su SHA-256, si el runner lo publica;
- logs que identifiquen RTX 5090, Torch 2.11.0+cu128 y NATTEN 0.21.6;
- grupo de Salad nuevamente en `stopped`, sin instancias ni cambios pendientes.
