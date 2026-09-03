[CmdletBinding()]
param(
    [ValidateSet("Status", "Start", "Monitor", "Download", "Stop", "RecoverKeyframe")]
    [string]$Action = "Status",

    [ValidateRange(5, 1440)]
    [int]$TimeoutMinutes = 720,

    [string]$OutputDirectory = ".\data\output\phase7\benchmarks\rtx5090-cloud"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:Organization = "yagobordellorg"
$script:Project = "aivideofactory"
$script:GroupName = "ai-video-factory-bench-rtx5090"
$script:MinimumVersion = 10
$script:ExpectedImage = (
    "docker.io/yagobordell/ai-video-factory-benchmark:" +
    "phase7-ltx25-torch211-cu128-natten0216-v8-salad"
)
$script:BadMachineIds = @(
    "8e9fc285-4a97-145d-b192-faed64f70e29"
)
$script:ContainersBase = (
    "https://api.salad.com/api/public/organizations/" +
    "$($script:Organization)/projects/$($script:Project)/containers"
)

function Get-OptionalProperty {
    param(
        [AllowNull()][object]$Object,
        [Parameter(Mandatory)][string]$Name,
        [AllowNull()][object]$Default = $null
    )

    if ($null -eq $Object) {
        return $Default
    }

    $Property = $Object.PSObject.Properties[$Name]
    if ($null -eq $Property) {
        return $Default
    }

    return $Property.Value
}

function Initialize-SaladSession {
    $ApiKey = [Environment]::GetEnvironmentVariable(
        "SALAD_API_KEY",
        [EnvironmentVariableTarget]::Process
    )

    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        $SecureKey = Read-Host "Salad API key (solo se conservará en memoria)" -AsSecureString
        $Credential = [PSCredential]::new("salad", $SecureKey)
        $ApiKey = $Credential.GetNetworkCredential().Password
    }

    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        throw "SALAD_API_KEY está vacía."
    }

    $script:Headers = @{
        "Salad-Api-Key" = $ApiKey
        "Accept"        = "application/json"
        "User-Agent"    = "ai-video-factory-phase7-resume/1.0"
    }
}

function Get-ContainerGroup {
    return Invoke-RestMethod `
        -Uri "$($script:ContainersBase)/$($script:GroupName)" `
        -Headers $script:Headers `
        -TimeoutSec 30
}

function Get-ContainerInstances {
    $Response = Invoke-RestMethod `
        -Uri "$($script:ContainersBase)/$($script:GroupName)/instances" `
        -Headers $script:Headers `
        -TimeoutSec 30

    $Items = Get-OptionalProperty $Response "instances" @()
    return @($Items)
}

function Get-CurrentInstance {
    param(
        [Parameter(Mandatory)][object]$Group,
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [object[]]$Instances
    )

    if ($Instances.Count -eq 0) {
        return $null
    }

    $GroupVersion = [int](Get-OptionalProperty $Group "version" -1)
    $Current = @(
        $Instances | Where-Object {
            [int](Get-OptionalProperty $_ "version" -1) -eq $GroupVersion
        }
    )

    if ($Current.Count -eq 0) {
        $Current = $Instances
    }

    return $Current |
        Sort-Object {
            [string](Get-OptionalProperty $_ "update_time" "")
        } -Descending |
        Select-Object -First 1
}

function Get-GroupStatus {
    param([Parameter(Mandatory)][object]$Group)

    $CurrentState = Get-OptionalProperty $Group "current_state"
    return [string](Get-OptionalProperty $CurrentState "status" "unknown")
}

function Get-ResultsBaseUrl {
    param([Parameter(Mandatory)][object]$Group)

    $Networking = Get-OptionalProperty $Group "networking"
    $Dns = [string](Get-OptionalProperty $Networking "dns" "")
    if ([string]::IsNullOrWhiteSpace($Dns)) {
        throw "El Container Group no publica un nombre DNS."
    }

    return "https://$Dns"
}

function Assert-Checkpoint {
    param([Parameter(Mandatory)][object]$Group)

    $Version = [int](Get-OptionalProperty $Group "version" -1)
    $Pending = [bool](Get-OptionalProperty $Group "pending_change" $false)
    $Container = Get-OptionalProperty $Group "container"
    $Image = [string](Get-OptionalProperty $Container "image" "")

    if ($Version -lt $script:MinimumVersion) {
        throw "Se esperaba al menos la versión $($script:MinimumVersion); Salad devuelve $Version."
    }
    if ($Pending) {
        throw "El grupo tiene un cambio pendiente. Espera a que termine antes de arrancarlo."
    }
    if ($Image -ne $script:ExpectedImage) {
        throw "Imagen inesperada. Esperada: $($script:ExpectedImage). Recibida: $Image"
    }
}

function Show-Phase7Status {
    $Group = Get-ContainerGroup
    $Instances = @(Get-ContainerInstances)
    $Instance = Get-CurrentInstance -Group $Group -Instances $Instances
    $Container = Get-OptionalProperty $Group "container"

    [pscustomobject]@{
        Name          = Get-OptionalProperty $Group "name"
        Version       = Get-OptionalProperty $Group "version"
        PendingChange = Get-OptionalProperty $Group "pending_change"
        Status        = Get-GroupStatus $Group
        Replicas      = Get-OptionalProperty $Group "replicas"
        Image         = Get-OptionalProperty $Container "image"
        InstanceId    = Get-OptionalProperty $Instance "id"
        InstanceState = Get-OptionalProperty $Instance "state" "sin-instancia"
        Ready         = Get-OptionalProperty $Instance "ready" $false
        MachineId     = Get-OptionalProperty $Instance "machine_id"
    } | Format-List
}

function Invoke-ReallocateIfBlocked {
    param([AllowNull()][object]$Instance)

    if ($null -eq $Instance) {
        return $false
    }

    $MachineId = [string](Get-OptionalProperty $Instance "machine_id" "")
    if ($script:BadMachineIds -notcontains $MachineId) {
        return $false
    }

    $InstanceId = [string](Get-OptionalProperty $Instance "id" "")
    if ([string]::IsNullOrWhiteSpace($InstanceId)) {
        throw "Salad asignó el nodo bloqueado, pero no devolvió el ID de instancia."
    }

    Write-Warning "Nodo $MachineId bloqueado por exits 139; solicitando reallocation."
    Invoke-RestMethod `
        -Method Post `
        -Uri "$($script:ContainersBase)/$($script:GroupName)/instances/$InstanceId/reallocate" `
        -Headers $script:Headers `
        -TimeoutSec 30 |
        Out-Null

    return $true
}

function Start-Phase7Benchmark {
    $Group = Get-ContainerGroup
    Assert-Checkpoint $Group

    $Status = Get-GroupStatus $Group
    if ($Status -eq "stopped") {
        Write-Host "Arrancando $($script:GroupName)..." -ForegroundColor Cyan
        Invoke-RestMethod `
            -Method Post `
            -Uri "$($script:ContainersBase)/$($script:GroupName)/start" `
            -Headers $script:Headers `
            -TimeoutSec 30 |
            Out-Null
    }
    elseif ($Status -notin @("pending", "deploying", "running")) {
        throw "El grupo está en un estado no arrancable: $Status"
    }

    $Deadline = (Get-Date).AddMinutes([Math]::Min($TimeoutMinutes, 180))
    $Reallocated = @{}

    do {
        $Group = Get-ContainerGroup
        $Instances = @(Get-ContainerInstances)
        $Instance = Get-CurrentInstance -Group $Group -Instances $Instances
        $InstanceId = [string](Get-OptionalProperty $Instance "id" "")
        $State = [string](Get-OptionalProperty $Instance "state" "sin-instancia")
        $MachineId = [string](Get-OptionalProperty $Instance "machine_id" "")
        $MachineLabel = if ($MachineId) { $MachineId } else { "no-asignada" }

        Write-Host (
            "{0} group={1} instance={2} machine={3}" -f `
            (Get-Date -Format "HH:mm:ss"),
            (Get-GroupStatus $Group),
            $State,
            $MachineLabel
        )

        if (
            $InstanceId -and
            -not $Reallocated.ContainsKey($InstanceId) -and
            (Invoke-ReallocateIfBlocked -Instance $Instance)
        ) {
            $Reallocated[$InstanceId] = $true
            Start-Sleep -Seconds 30
            continue
        }

        if ($null -ne $Instance -and $MachineId -and $script:BadMachineIds -notcontains $MachineId) {
            Write-Host "Instancia válida asignada. Ejecuta -Action Monitor." -ForegroundColor Green
            return
        }

        Start-Sleep -Seconds 30
    }
    while ((Get-Date) -lt $Deadline)

    throw "No se obtuvo una instancia válida antes del timeout. Ejecuta -Action Stop."
}

function Get-BenchmarkStatus {
    param([Parameter(Mandatory)][string]$BaseUrl)

    try {
        return Invoke-RestMethod `
            -Uri "$BaseUrl/results/status.json" `
            -TimeoutSec 20
    }
    catch {
        return $null
    }
}

function Watch-Phase7Benchmark {
    $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $Reallocated = @{}

    do {
        $Group = Get-ContainerGroup
        Assert-Checkpoint $Group
        $Instances = @(Get-ContainerInstances)
        $Instance = Get-CurrentInstance -Group $Group -Instances $Instances

        $InstanceId = [string](Get-OptionalProperty $Instance "id" "")
        if (
            $InstanceId -and
            -not $Reallocated.ContainsKey($InstanceId) -and
            (Invoke-ReallocateIfBlocked -Instance $Instance)
        ) {
            $Reallocated[$InstanceId] = $true
            Start-Sleep -Seconds 30
            continue
        }

        $InstanceState = [string](Get-OptionalProperty $Instance "state" "sin-instancia")
        $Ready = [bool](Get-OptionalProperty $Instance "ready" $false)
        $Benchmark = $null
        try {
            $BaseUrl = Get-ResultsBaseUrl $Group
            $Benchmark = Get-BenchmarkStatus -BaseUrl $BaseUrl
        }
        catch {
            $Benchmark = $null
        }

        $BenchmarkState = [string](Get-OptionalProperty $Benchmark "status" "no-disponible")
        Write-Host (
            "{0} group={1} instance={2} ready={3} benchmark={4}" -f `
            (Get-Date -Format "HH:mm:ss"),
            (Get-GroupStatus $Group),
            $InstanceState,
            $Ready,
            $BenchmarkState
        )

        if ($BenchmarkState -eq "succeeded") {
            Write-Host "Benchmark terminado. Ejecuta -Action Download y después -Action Stop." -ForegroundColor Green
            return
        }
        if ($BenchmarkState -eq "failed") {
            $Message = [string](Get-OptionalProperty $Benchmark "message" "sin detalle")
            throw "El benchmark falló: $Message"
        }
        if ((Get-GroupStatus $Group) -eq "stopped" -and $Instances.Count -eq 0) {
            throw "El grupo se detuvo antes de publicar un resultado exitoso."
        }

        Start-Sleep -Seconds 30
    }
    while ((Get-Date) -lt $Deadline)

    throw "Monitorización agotada tras $TimeoutMinutes minutos. Ejecuta -Action Stop."
}

function Save-RemoteFile {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][string]$RemotePath,
        [Parameter(Mandatory)][string]$LocalPath,
        [switch]$Optional
    )

    $TemporaryPath = "$LocalPath.download"
    try {
        Invoke-WebRequest `
            -Uri "$BaseUrl/$RemotePath" `
            -OutFile $TemporaryPath `
            -UseBasicParsing `
            -TimeoutSec 300

        if ((Get-Item -LiteralPath $TemporaryPath).Length -le 0) {
            throw "La descarga está vacía."
        }

        Move-Item -LiteralPath $TemporaryPath -Destination $LocalPath -Force
        Write-Host "Descargado: $LocalPath"
    }
    catch {
        Remove-Item -LiteralPath $TemporaryPath -Force -ErrorAction SilentlyContinue
        if ($Optional) {
            Write-Warning "No disponible: $RemotePath"
            return
        }
        throw
    }
}

function Save-Phase7Benchmark {
    $Group = Get-ContainerGroup
    Assert-Checkpoint $Group
    $BaseUrl = Get-ResultsBaseUrl $Group
    $Status = Get-BenchmarkStatus -BaseUrl $BaseUrl
    $BenchmarkState = [string](Get-OptionalProperty $Status "status" "no-disponible")
    if ($BenchmarkState -ne "succeeded") {
        throw "El resultado todavía no está listo: $BenchmarkState"
    }

    $Root = [IO.Path]::GetFullPath($OutputDirectory)
    New-Item -ItemType Directory -Path $Root -Force | Out-Null

    $Downloads = @(
        @{ Remote = "results/status.json"; Local = "status.json"; Optional = $false }
        @{ Remote = "results/benchmark.log"; Local = "benchmark.log"; Optional = $false }
        @{ Remote = "results/rtx5090/matrix.json"; Local = "matrix.json"; Optional = $false }
        @{ Remote = "results/rtx5090/distilled-fp8-cpu.json"; Local = "distilled-fp8-cpu.json"; Optional = $false }
        @{ Remote = "results/videos/rtx5090/distilled-fp8-cpu.mp4"; Local = "distilled-fp8-cpu.mp4"; Optional = $false }
    )

    foreach ($Download in $Downloads) {
        Save-RemoteFile `
            -BaseUrl $BaseUrl `
            -RemotePath $Download.Remote `
            -LocalPath (Join-Path $Root $Download.Local) `
            -Optional:([bool]$Download.Optional)
    }

    Get-Content -LiteralPath (Join-Path $Root "matrix.json") -Raw |
        ConvertFrom-Json |
        Out-Null
    Get-Content -LiteralPath (Join-Path $Root "distilled-fp8-cpu.json") -Raw |
        ConvertFrom-Json |
        Out-Null

    $VideoPath = Join-Path $Root "distilled-fp8-cpu.mp4"
    if (Test-Path -LiteralPath $VideoPath) {
        Get-FileHash -Algorithm SHA256 -LiteralPath $VideoPath |
            Select-Object Path, Hash |
            Format-List
    }

    Write-Host "Resultados validados en $Root" -ForegroundColor Green
}

function Stop-Phase7Benchmark {
    $Group = Get-ContainerGroup
    $Status = Get-GroupStatus $Group

    if ($Status -ne "stopped") {
        Write-Host "Deteniendo $($script:GroupName)..." -ForegroundColor Cyan
        Invoke-RestMethod `
            -Method Post `
            -Uri "$($script:ContainersBase)/$($script:GroupName)/stop" `
            -Headers $script:Headers `
            -TimeoutSec 30 |
            Out-Null
    }

    $Deadline = (Get-Date).AddMinutes(20)
    do {
        $Group = Get-ContainerGroup
        $Instances = @(Get-ContainerInstances)
        $Status = Get-GroupStatus $Group
        Write-Host "$(Get-Date -Format 'HH:mm:ss') status=$Status instances=$($Instances.Count)"

        if ($Status -eq "stopped" -and $Instances.Count -eq 0) {
            Write-Host "Grupo detenido; ya no debe consumir GPU." -ForegroundColor Green
            return
        }

        Start-Sleep -Seconds 20
    }
    while ((Get-Date) -lt $Deadline)

    throw "Salad no confirmó la detención dentro de 20 minutos. Revisa el portal."
}

function Restore-CanonicalKeyframe {
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker no está instalado o no está en PATH."
    }

    $TargetDirectory = Join-Path $PSScriptRoot "..\docker\phase7-benchmark\assets"
    $TargetDirectory = [IO.Path]::GetFullPath($TargetDirectory)
    $TargetPath = Join-Path $TargetDirectory "shot_001.png"
    $RecoveryContainer = "ai-video-factory-phase7-recovery"
    New-Item -ItemType Directory -Path $TargetDirectory -Force | Out-Null

    & docker container inspect $RecoveryContainer *> $null
    if ($LASTEXITCODE -eq 0) {
        throw "Ya existe el contenedor temporal $RecoveryContainer; elimínalo manualmente y repite."
    }

    Write-Host "Descargando la imagen de recuperación; puede tardar varios minutos..." -ForegroundColor Cyan
    & docker pull $script:ExpectedImage
    if ($LASTEXITCODE -ne 0) {
        throw "docker pull falló. Inicia Docker Desktop y ejecuta docker login si el repositorio es privado."
    }

    $Created = $false
    try {
        & docker create --name $RecoveryContainer $script:ExpectedImage | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "docker create falló."
        }
        $Created = $true

        $ContainerSource = (
            "${RecoveryContainer}:" +
            "/opt/factory/docker/phase7-benchmark/assets/shot_001.png"
        )
        & docker cp $ContainerSource $TargetPath
        if ($LASTEXITCODE -ne 0) {
            throw "No se pudo extraer shot_001.png de la imagen v8."
        }
    }
    finally {
        if ($Created) {
            & docker rm -f $RecoveryContainer | Out-Null
        }
    }

    if (-not (Test-Path -LiteralPath $TargetPath)) {
        throw "El keyframe no quedó guardado."
    }
    if ((Get-Item -LiteralPath $TargetPath).Length -le 0) {
        throw "El keyframe recuperado está vacío."
    }

    Get-FileHash -Algorithm SHA256 -LiteralPath $TargetPath |
        Select-Object Path, Hash |
        Format-List
    Write-Host "Keyframe recuperado correctamente." -ForegroundColor Green
}

if ($Action -eq "RecoverKeyframe") {
    Restore-CanonicalKeyframe
    exit 0
}

Initialize-SaladSession

switch ($Action) {
    "Status" {
        Show-Phase7Status
    }
    "Start" {
        Start-Phase7Benchmark
    }
    "Monitor" {
        Watch-Phase7Benchmark
    }
    "Download" {
        Save-Phase7Benchmark
    }
    "Stop" {
        Stop-Phase7Benchmark
    }
}
