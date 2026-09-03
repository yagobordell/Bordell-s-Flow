# Recuperar el checkpoint cerrado de la Fase 7

Estado de cierre del **3 de septiembre de 2026 UTC**:

| Campo | Valor |
|---|---|
| Container Group | `ai-video-factory-bench-rtx5090` |
| Versión | `10` |
| Estado final | `stopped`, sin instancias |
| Imagen | `docker.io/yagobordell/ai-video-factory-benchmark:phase7-ltx25-torch211-cu128-natten0216-v8-salad` |
| GPU | RTX 5090, clase `851399fb-7329-4195-a042-d6514b28cf33` |
| Perfil validado | `distilled-fp8-cpu` |
| Ejecuciones | 1 warmup + 3 medidas |
| Resultado | `succeeded` |

La Fase 7 ya está cerrada. **No hay que arrancar el Container Group, reconstruir la imagen ni
repetir el benchmark** para continuar el proyecto.

## Preparar otro Windows

Instala Git, abre una PowerShell nueva y clona el repositorio:

```powershell
winget install --id Git.Git -e
Set-Location "$HOME\Downloads"
git clone https://github.com/yagobordell/ai-video-factory.git
Set-Location ".\ai-video-factory"
git status
git log -1 --oneline
```

Para desarrollar la Fase 8 instala Python 3.12 y las dependencias:

```powershell
winget install --id Python.Python.3.12 -e
python -m pip install -e ".[gpu,dev]"
python -m pytest
python -m ruff check .
```

Docker Desktop solo es necesario si vas a construir o probar imágenes localmente:

```powershell
winget install --id Docker.DockerDesktop -e
```

## Artefactos locales

Los outputs están ignorados por Git y permanecen en el equipo que ejecutó la descarga:

```text
data/output/phase7/benchmarks/rtx5090-cloud/
├── status.json
├── benchmark.log
├── matrix.json
├── distilled-fp8-cpu.json
└── distilled-fp8-cpu.mp4
```

Si cambias de ordenador, copia esa carpeta de forma privada. Comprueba el vídeo con:

```powershell
Get-FileHash `
    ".\data\output\phase7\benchmarks\rtx5090-cloud\distilled-fp8-cpu.mp4" `
    -Algorithm SHA256
```

El resultado esperado es
`6121925F18EE134278FC2FB1069B334D0779AAF6A3D1D763CAFBCFA6CCE62B0C`.
Los demás hashes están en [`phase7-closure.md`](phase7-closure.md).

La ausencia de esos archivos en otro ordenador no reabre la fase: Git conserva la ficha de
evidencia y sus hashes. Repite la ejecución cloud solo si necesitas regenerar el binario o comparar
un workload/hardware distinto.

## Consulta opcional de Salad

Para confirmar que el grupo sigue detenido, carga la API key únicamente en memoria:

```powershell
$SecureSaladKey = Read-Host "Salad API key" -AsSecureString
$env:SALAD_API_KEY = (
    [PSCredential]::new("salad", $SecureSaladKey)
).GetNetworkCredential().Password
Set-ExecutionPolicy -Scope Process Bypass -Force
.\scripts\resume_phase7_benchmark.ps1 -Action Status
```

La comprobación esperada es versión 10, imagen v8, `Status=stopped` y ninguna instancia. No uses
`-Action Start` salvo que hayas decidido repetir deliberadamente el benchmark y aceptar el coste de
GPU.

## Siguiente paso

Continúa con la Fase 8: registrar la tarea de generación LTX-2.5 en el worker existente, conservar
los contratos idempotentes de Postgres/R2 y usar el baseline RTX 5090 + `fp8-cast` + offload a CPU.
