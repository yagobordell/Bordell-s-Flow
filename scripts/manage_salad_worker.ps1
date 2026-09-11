[CmdletBinding()]
param(
    [ValidateSet("Prepare", "Start", "Status", "Stop")]
    [string]$Action = "Status",

    [string]$Service = "ltx25",

    [string]$Image = "",

    [ValidateRange(10, 180)]
    [int]$PrepareTimeoutMinutes = 120,

    [switch]$SkipBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ServicesPath = Join-Path $RepoRoot "deploy\salad\services.json"
if (-not (Test-Path -LiteralPath $ServicesPath -PathType Leaf)) {
    throw "Salad service manifest not found: $ServicesPath"
}

$Document = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$ServiceProperty = $Document.services.PSObject.Properties[$Service]
if ($null -eq $ServiceProperty) {
    $Available = @($Document.services.PSObject.Properties.Name) -join ", "
    throw "Unknown Salad service '$Service'. Available services: $Available"
}
$Definition = $ServiceProperty.Value

$Organization = [string]$Definition.organization
$Project = [string]$Definition.project
$GroupName = [string]$Definition.group_name
$QueueName = [string]$Definition.queue_name
$Dockerfile = Join-Path $RepoRoot ([string]$Definition.dockerfile)
if ([string]::IsNullOrWhiteSpace($Image)) {
    $Image = [string]$Definition.image
}

$ApiBase = "https://api.salad.com/api/public/organizations/$Organization/projects/$Project"
$ContainersBase = "$ApiBase/containers"
$QueuesBase = "$ApiBase/queues"
$SecretNames = @(
    "POSTGRES_DSN",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "HF_TOKEN"
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
        return $Value.Trim()
    }

    if ($Secret) {
        $SecureValue = Read-Host $Prompt -AsSecureString
        $Credential = [PSCredential]::new("salad-worker", $SecureValue)
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
        "User-Agent" = "ai-video-factory-worker-manager/1.0"
    }
}

function Get-HttpStatusCode {
    param([Parameter(Mandatory)][object]$ErrorRecord)

    $Response = $ErrorRecord.Exception.Response
    if ($null -eq $Response) {
        return $null
    }
    try {
        return [int]$Response.StatusCode
    }
    catch {
        return $null
    }
}

function Ensure-Queue {
    param([Parameter(Mandatory)][hashtable]$Headers)

    try {
        Invoke-RestMethod `
            -Uri "$QueuesBase/$QueueName" `
            -Headers $Headers `
            -TimeoutSec 30 |
            Out-Null
        Write-Host "Queue exists: $QueueName" -ForegroundColor Green
        return
    }
    catch {
        if ((Get-HttpStatusCode -ErrorRecord $_) -ne 404) {
            throw
        }
    }

    $QueueBody = @{
        name = $QueueName
        display_name = "$Service jobs"
        description = "AI Video Factory jobs for the $Service inference worker"
    } | ConvertTo-Json

    Write-Host "Creating queue: $QueueName" -ForegroundColor Cyan
    Invoke-RestMethod `
        -Method Post `
        -Uri $QueuesBase `
        -Headers $Headers `
        -ContentType "application/json" `
        -Body $QueueBody `
        -TimeoutSec 60 |
        Out-Null
}

function Get-Group {
    param([Parameter(Mandatory)][hashtable]$Headers)

    try {
        return Invoke-RestMethod `
            -Uri "$ContainersBase/$GroupName" `
            -Headers $Headers `
            -TimeoutSec 30
    }
    catch {
        if ((Get-HttpStatusCode -ErrorRecord $_) -eq 404) {
            throw (
                "Container group '$GroupName' does not exist. " +
                "Create the deployment slot in Salad before preparing service '$Service'."
            )
        }
        throw
    }
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
            "{0} service={1} status={2} replicas={3} pending={4} description={5}" -f `
            (Get-Date -Format "HH:mm:ss"),
            $Service,
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

function New-Probe {
    param([Parameter(Mandatory)][object]$Probe)

    return @{
        http = @{
            headers = @()
            path = [string]$Probe.path
            port = 8080
            scheme = "http"
        }
        initial_delay_seconds = 0
        period_seconds = [int]$Probe.period_seconds
        failure_threshold = [int]$Probe.failure_threshold
        success_threshold = 1
        timeout_seconds = [int]$Probe.timeout_seconds
    }
}

function Get-WorkerEnvironment {
    $Environment = @{}
    foreach ($Property in $Definition.environment.PSObject.Properties) {
        $Override = [Environment]::GetEnvironmentVariable(
            $Property.Name,
            [EnvironmentVariableTarget]::Process
        )
        if ([string]::IsNullOrWhiteSpace($Override)) {
            $Environment[$Property.Name] = [string]$Property.Value
        }
        else {
            $Environment[$Property.Name] = $Override.Trim()
        }
    }

    foreach ($Name in @($Definition.required_secrets)) {
        $IsSecret = $SecretNames -contains [string]$Name
        $Environment[[string]$Name] = Get-Setting `
            -Name ([string]$Name) `
            -Prompt ([string]$Name) `
            -Secret:$IsSecret
    }
    return $Environment
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
            @{Name = "Service"; Expression = {$Service}},
            @{Name = "Queue"; Expression = {$QueueName}},
            @{Name = "Status"; Expression = {$_.current_state.status}},
            @{Name = "Description"; Expression = {$_.current_state.description}},
            @{Name = "Image"; Expression = {$_.container.image}},
            @{Name = "CPU"; Expression = {$_.container.resources.cpu}},
            @{Name = "MemoryMiB"; Expression = {$_.container.resources.memory}},
            @{Name = "GPUClasses"; Expression = {$_.container.resources.gpu_classes -join ","}} |
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

Set-Location $RepoRoot
$Headers = Get-Headers

switch ($Action) {
    "Status" {
        Show-Status -Headers $Headers
        exit 0
    }

    "Stop" {
        $Group = Get-Group -Headers $Headers
        if ((Get-GroupStatus -Group $Group) -eq "stopped") {
            Write-Host "$Service worker is already stopped." -ForegroundColor Green
            exit 0
        }
        Invoke-RestMethod `
            -Method Post `
            -Uri "$ContainersBase/$GroupName/stop" `
            -Headers $Headers `
            -TimeoutSec 60 |
            Out-Null
        Wait-ForGroupStatus -Headers $Headers -Expected "stopped" | Out-Null
        Write-Host "$Service worker stopped." -ForegroundColor Green
        exit 0
    }

    "Start" {
        Ensure-Queue -Headers $Headers
        $Group = Get-Group -Headers $Headers
        if ((Get-GroupStatus -Group $Group) -eq "running") {
            Write-Host "$Service worker is already running." -ForegroundColor Green
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
        Write-Host "Container group is running; model readiness may still be in progress." `
            -ForegroundColor Cyan
        Show-Status -Headers $Headers
        exit 0
    }

    "Prepare" {
        Ensure-Queue -Headers $Headers
        $Group = Get-Group -Headers $Headers
        if ((Get-GroupStatus -Group $Group) -ne "stopped") {
            throw "Container group must be stopped before Prepare. Run -Action Stop first."
        }

        if (-not $SkipBuild) {
            & docker version *> $null
            if ($LASTEXITCODE -ne 0) {
                throw "Docker Desktop is not running."
            }
            Write-Host "Building and publishing $Image" -ForegroundColor Cyan
            & docker buildx build `
                --platform linux/amd64 `
                --file $Dockerfile `
                --tag $Image `
                --push `
                .
            if ($LASTEXITCODE -ne 0) {
                throw "Docker build or push failed."
            }
        }

        $PinnedImage = Resolve-PinnedImage -MutableImage $Image
        Write-Host "Pinned image: $PinnedImage" -ForegroundColor Green
        $WorkerEnvironment = Get-WorkerEnvironment
        $PreviousVersion = [int]$Group.version

        $PatchBody = @{
            replicas = 0
            priority = [string]$Definition.priority
            container = @{
                image = $PinnedImage
                resources = @{
                    cpu = [int]$Definition.resources.cpu
                    memory = [int]$Definition.resources.memory
                    gpu_classes = @($Definition.resources.gpu_classes)
                    shm_size = [int]$Definition.resources.shm_size
                    storage_amount = [Int64]$Definition.resources.storage_amount
                }
                environment_variables = $WorkerEnvironment
                image_caching = $true
            }
            startup_probe = New-Probe -Probe $Definition.probes.startup
            readiness_probe = New-Probe -Probe $Definition.probes.readiness
            liveness_probe = New-Probe -Probe $Definition.probes.liveness
            queue_connection = @{
                path = "/jobs"
                port = 8080
                queue_name = $QueueName
            }
            queue_autoscaler = @{
                min_replicas = [int]$Definition.autoscaler.min_replicas
                max_replicas = [int]$Definition.autoscaler.max_replicas
                desired_queue_length = [int]$Definition.autoscaler.desired_queue_length
                polling_period = [int]$Definition.autoscaler.polling_period
                max_upscale_per_minute = [int]$Definition.autoscaler.max_upscale_per_minute
                max_downscale_per_minute = [int]$Definition.autoscaler.max_downscale_per_minute
            }
        } | ConvertTo-Json -Depth 20

        Write-Host "Submitting $Service worker upgrade..." -ForegroundColor Cyan
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
                "{0} service={1} version={2} pending={3} status={4} description={5}" -f `
                (Get-Date -Format "HH:mm:ss"),
                $Service,
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
        if ([string]$Group.queue_connection.queue_name -ne $QueueName) {
            throw "Salad did not activate the expected queue: $QueueName"
        }

        $PatchBody = $null
        $WorkerEnvironment = $null

        Write-Host "$Service worker image/config prepared; group remains stopped." `
            -ForegroundColor Green
        Show-Status -Headers $Headers
    }
}
