[CmdletBinding()]
param(
    [ValidateSet("Prepare", "Start", "Status", "Stop")]
    [string]$Action = "Status",

    [string]$Image = (
        "docker.io/yagobordell/ai-video-factory:" +
        "phase8-ltx25-torch211-cu128-natten0216-v1"
    ),

    [ValidateRange(10, 180)]
    [int]$PrepareTimeoutMinutes = 120,

    [switch]$SkipBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Organization = "yagobordellorg"
$Project = "aivideofactory"
$GroupName = "ai-video-factory-worker"
$QueueName = "ai-video-factory-jobs"
$GpuClassId = "851399fb-7329-4195-a042-d6514b28cf33"
$ContainersBase = (
    "https://api.salad.com/api/public/organizations/" +
    "$Organization/projects/$Project/containers"
)

function Get-Setting {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Prompt,
        [switch]$Secret
    )

    $Value = [Environment]::GetEnvironmentVariable(
        $Name,
        [EnvironmentVariableTarget]::Process
    )
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value
    }

    if ($Secret) {
        $SecureValue = Read-Host $Prompt -AsSecureString
        $Credential = [PSCredential]::new("phase8", $SecureValue)
        $Value = $Credential.GetNetworkCredential().Password
    }
    else {
        $Value = Read-Host $Prompt
    }

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is empty."
    }
    return $Value.Trim()
}

function Get-Headers {
    $ApiKey = Get-Setting -Name "SALAD_API_KEY" -Prompt "Salad API key" -Secret
    return @{
        "Salad-Api-Key" = $ApiKey
        "Accept" = "application/json"
        "User-Agent" = "ai-video-factory-phase8/1.0"
    }
}

function Get-Group {
    param([Parameter(Mandatory)][hashtable]$Headers)

    return Invoke-RestMethod `
        -Uri "$ContainersBase/$GroupName" `
        -Headers $Headers `
        -TimeoutSec 30
}

function Get-GroupStatus {
    param([Parameter(Mandatory)][object]$Group)

    if ($null -eq $Group.current_state) {
        return "unknown"
    }
    return [string]$Group.current_state.status
}

function Wait-ForGroupStatus {
    param(
        [Parameter(Mandatory)][hashtable]$Headers,
        [Parameter(Mandatory)][string]$Expected,
        [ValidateRange(1, 180)][int]$TimeoutMinutes = 30
    )

    $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    do {
        Start-Sleep -Seconds 15
        $Group = Get-Group -Headers $Headers
        $Status = Get-GroupStatus -Group $Group
        Write-Host (
            "{0} status={1} replicas={2} pending={3} description={4}" -f `
            (Get-Date -Format "HH:mm:ss"),
            $Status,
            $Group.replicas,
            $Group.pending_change,
            [string]$Group.current_state.description
        )
        if ($Status -eq $Expected -and -not $Group.pending_change) {
            return $Group
        }
    }
    while ((Get-Date) -lt $Deadline)

    throw "Container group did not reach status '$Expected' before timeout."
}

function Resolve-PinnedImage {
    param([Parameter(Mandatory)][string]$MutableImage)

    $Inspect = & docker buildx imagetools inspect $MutableImage 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Published image cannot be inspected: $MutableImage"
    }

    $Digest = $null
    foreach ($Line in $Inspect) {
        if ([string]$Line -match '^\s*Digest:\s+(sha256:[0-9a-f]{64})\s*$') {
            $Digest = $Matches[1]
            break
        }
    }
    if ($null -eq $Digest) {
        throw "Could not resolve image digest from docker buildx imagetools inspect."
    }
    if ($MutableImage -notmatch '^(?<repo>.+):[^/:]+$') {
        throw "Image must include an explicit tag: $MutableImage"
    }
    return "$($Matches['repo'])@$Digest"
}

function Show-Status {
    param([Parameter(Mandatory)][hashtable]$Headers)

    $Group = Get-Group -Headers $Headers
    $Group |
        Select-Object `
            name,
            version,
            replicas,
            priority,
            pending_change,
            @{Name = "Status"; Expression = {$_.current_state.status}},
            @{Name = "Description"; Expression = {$_.current_state.description}},
            @{Name = "Image"; Expression = {$_.container.image}},
            @{Name = "CPU"; Expression = {$_.container.resources.cpu}},
            @{Name = "MemoryMiB"; Expression = {$_.container.resources.memory}},
            @{Name = "GPUClasses"; Expression = {$_.container.resources.gpu_classes -join ","}},
            @{Name = "StorageBytes"; Expression = {$_.container.resources.storage_amount}},
            @{Name = "LivenessPeriodSeconds"; Expression = {$_.liveness_probe.period_seconds}},
            @{Name = "LivenessTimeoutSeconds"; Expression = {$_.liveness_probe.timeout_seconds}},
            @{Name = "LivenessFailureThreshold"; Expression = {$_.liveness_probe.failure_threshold}} |
        Format-List

    try {
        $Response = Invoke-RestMethod `
            -Uri "$ContainersBase/$GroupName/instances" `
            -Headers $Headers `
            -TimeoutSec 30

        $InstanceRows = @()
        if ($Response.PSObject.Properties.Name -contains "instances") {
            $InstanceRows = @($Response.instances)
        }
        elseif ($Response.PSObject.Properties.Name -contains "items") {
            $InstanceRows = @($Response.items)
        }

        if ($InstanceRows.Count -gt 0) {
            $InstanceRows |
                Select-Object `
                    id,
                    machine_id,
                    state,
                    pulling_progress,
                    ready,
                    started,
                    update_time |
                Format-Table -AutoSize
        }
    }
    catch {
        Write-Warning "Could not list container instances: $($_.Exception.Message)"
    }
}

Set-Location ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..")))
$Headers = Get-Headers

switch ($Action) {
    "Status" {
        Show-Status -Headers $Headers
        exit 0
    }

    "Stop" {
        $Group = Get-Group -Headers $Headers
        if ((Get-GroupStatus -Group $Group) -eq "stopped") {
            Write-Host "Phase 8 worker is already stopped." -ForegroundColor Green
            exit 0
        }
        Invoke-RestMethod `
            -Method Post `
            -Uri "$ContainersBase/$GroupName/stop" `
            -Headers $Headers `
            -TimeoutSec 60 |
            Out-Null
        Wait-ForGroupStatus -Headers $Headers -Expected "stopped" | Out-Null
        Write-Host "Phase 8 worker stopped." -ForegroundColor Green
        exit 0
    }

    "Start" {
        $Group = Get-Group -Headers $Headers
        if ((Get-GroupStatus -Group $Group) -eq "running") {
            Write-Host "Phase 8 worker is already running." -ForegroundColor Green
            Show-Status -Headers $Headers
            exit 0
        }
        Invoke-RestMethod `
            -Method Post `
            -Uri "$ContainersBase/$GroupName/start" `
            -Headers $Headers `
            -TimeoutSec 60 |
            Out-Null
        Wait-ForGroupStatus -Headers $Headers -Expected "running" | Out-Null
        Write-Host "Container group is running; model bootstrap/readiness may still be in progress." `
            -ForegroundColor Cyan
        Show-Status -Headers $Headers
        exit 0
    }

    "Prepare" {
        $Group = Get-Group -Headers $Headers
        if ((Get-GroupStatus -Group $Group) -ne "stopped") {
            throw "Container group must be stopped before Prepare. Run with -Action Stop first."
        }

        if (-not $SkipBuild) {
            & docker version *> $null
            if ($LASTEXITCODE -ne 0) {
                throw "Docker Desktop is not running."
            }
            Write-Host "Building and publishing $Image" -ForegroundColor Cyan
            & docker buildx build `
                --platform linux/amd64 `
                --file ".\docker\phase8-worker\Dockerfile" `
                --tag $Image `
                --push `
                .
            if ($LASTEXITCODE -ne 0) {
                throw "Docker build or push failed."
            }
        }

        $PinnedImage = Resolve-PinnedImage -MutableImage $Image
        Write-Host "Pinned image: $PinnedImage" -ForegroundColor Green

        $PostgresDsn = Get-Setting -Name "POSTGRES_DSN" -Prompt "Postgres DSN" -Secret
        $R2Endpoint = Get-Setting -Name "R2_ENDPOINT_URL" -Prompt "R2 endpoint URL"
        $R2Bucket = Get-Setting -Name "R2_BUCKET" -Prompt "R2 bucket"
        $R2AccessKey = Get-Setting -Name "R2_ACCESS_KEY_ID" -Prompt "R2 access key ID" -Secret
        $R2SecretKey = Get-Setting -Name "R2_SECRET_ACCESS_KEY" -Prompt "R2 secret access key" -Secret
        $HfToken = Get-Setting -Name "HF_TOKEN" -Prompt "Hugging Face token" -Secret

        $PreviousVersion = [int]$Group.version
        $PatchBody = @{
            replicas = 0
            priority = "medium"
            container = @{
                image = $PinnedImage
                resources = @{
                    cpu = 8
                    memory = 61440
                    gpu_classes = @($GpuClassId)
                    shm_size = 8192
                    storage_amount = 137438953472
                }
                environment_variables = @{
                    POSTGRES_DSN = $PostgresDsn
                    R2_ENDPOINT_URL = $R2Endpoint
                    R2_BUCKET = $R2Bucket
                    R2_ACCESS_KEY_ID = $R2AccessKey
                    R2_SECRET_ACCESS_KEY = $R2SecretKey
                    HF_TOKEN = $HfToken
                    GPU_WORKER_MODE = "production"
                    GPU_WORKER_RUNTIME = "phase8"
                    GPU_WORKER_LEASE_SECONDS = "900"
                    GPU_WORKER_HEARTBEAT_SECONDS = "30"
                    LTX_MODEL_ROOT = "/workspace/models/ltx-2.5"
                    LTX_MODEL_REPOSITORY = "Lightricks/LTX-2.5"
                    LTX_DEVICE = "cuda"
                    SALAD_QUEUE_ENABLED = "true"
                    SALAD_LOG_LEVEL = "info"
                }
                image_caching = $true
            }
            startup_probe = @{
                http = @{headers = @(); path = "/health"; port = 8080; scheme = "http"}
                initial_delay_seconds = 0
                period_seconds = 15
                failure_threshold = 20
                success_threshold = 1
                timeout_seconds = 5
            }
            readiness_probe = @{
                http = @{headers = @(); path = "/ready"; port = 8080; scheme = "http"}
                initial_delay_seconds = 0
                period_seconds = 10
                failure_threshold = 6
                success_threshold = 1
                timeout_seconds = 5
            }
            liveness_probe = @{
                http = @{headers = @(); path = "/health"; port = 8080; scheme = "http"}
                initial_delay_seconds = 0
                period_seconds = 30
                failure_threshold = 20
                success_threshold = 1
                timeout_seconds = 10
            }
            queue_connection = @{
                path = "/jobs"
                port = 8080
                queue_name = $QueueName
            }
            queue_autoscaler = @{
                min_replicas = 0
                max_replicas = 1
                desired_queue_length = 1
                polling_period = 30
                max_upscale_per_minute = 1
                max_downscale_per_minute = 1
            }
        } | ConvertTo-Json -Depth 20

        Write-Host "Submitting Phase 8 worker upgrade..." -ForegroundColor Cyan
        Invoke-RestMethod `
            -Method Patch `
            -Uri "$ContainersBase/$GroupName" `
            -Headers $Headers `
            -ContentType "application/merge-patch+json" `
            -Body $PatchBody `
            -TimeoutSec 60 |
            Out-Null

        $Deadline = (Get-Date).AddMinutes($PrepareTimeoutMinutes)
        do {
            Start-Sleep -Seconds 30
            $Group = Get-Group -Headers $Headers
            Write-Host (
                "{0} version={1} pending={2} status={3} description={4}" -f `
                (Get-Date -Format "HH:mm:ss"),
                $Group.version,
                $Group.pending_change,
                (Get-GroupStatus -Group $Group),
                [string]$Group.current_state.description
            )
        }
        while ($Group.pending_change -and (Get-Date) -lt $Deadline)

        if ($Group.pending_change) {
            throw "Salad did not finish preparing the image before the timeout."
        }
        if ($Group.container.image -ne $PinnedImage) {
            throw "Salad did not activate the expected image. Received: $($Group.container.image)"
        }
        if ([int]$Group.version -le $PreviousVersion) {
            throw "Container group version did not increase."
        }
        if ([string]$Group.priority -ne "medium") {
            throw "Salad did not activate the validated priority=medium configuration."
        }
        if ([int]$Group.liveness_probe.period_seconds -ne 30 -or
            [int]$Group.liveness_probe.timeout_seconds -ne 10 -or
            [int]$Group.liveness_probe.failure_threshold -ne 20) {
            throw "Salad did not activate the validated Phase 8 liveness configuration."
        }

        $PatchBody = $null
        $PostgresDsn = $null
        $R2SecretKey = $null
        $R2AccessKey = $null
        $HfToken = $null

        Write-Host "Phase 8 worker image/config prepared and group remains stopped." `
            -ForegroundColor Green
        Show-Status -Headers $Headers
    }
}
