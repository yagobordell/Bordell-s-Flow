[CmdletBinding()]
param(
    [ValidateSet("Validate", "Prepare", "Start", "Status", "Stop")]
    [string]$Action = "Status",

    [string]$Service = "ltx25",

    [string]$Image = "",

    [string]$EnvFile = ".env",

    [ValidateRange(10, 180)]
    [int]$PrepareTimeoutMinutes = 120,

    [switch]$SkipBuild,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ServicesPath = Join-Path $RepoRoot "deploy\salad\services.json"
if (-not (Test-Path -LiteralPath $ServicesPath -PathType Leaf)) {
    throw "Salad service manifest not found: $ServicesPath"
}

function Import-EnvFile {
    param([Parameter(Mandatory)][string]$Path)

    $Resolved = $Path
    if (-not [IO.Path]::IsPathRooted($Resolved)) {
        $Resolved = Join-Path $RepoRoot $Resolved
    }
    if (-not (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
        return
    }

    foreach ($RawLine in Get-Content -LiteralPath $Resolved) {
        $Line = $RawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($Line) -or $Line.StartsWith("#")) {
            continue
        }
        if ($Line -notmatch '^(?:export\s+)?(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$') {
            continue
        }

        $Name = $Matches["name"]
        $Value = $Matches["value"].Trim()
        if ($Value.Length -ge 2) {
            $First = $Value.Substring(0, 1)
            $Last = $Value.Substring($Value.Length - 1, 1)
            if (($First -eq '"' -and $Last -eq '"') -or ($First -eq "'" -and $Last -eq "'")) {
                $Value = $Value.Substring(1, $Value.Length - 2)
            }
        }

        $Existing = [Environment]::GetEnvironmentVariable(
            $Name,
            [EnvironmentVariableTarget]::Process
        )
        if ([string]::IsNullOrWhiteSpace($Existing)) {
            [Environment]::SetEnvironmentVariable(
                $Name,
                $Value,
                [EnvironmentVariableTarget]::Process
            )
        }
    }
}

Import-EnvFile -Path $EnvFile

$Document = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
$ServiceProperty = $Document.services.PSObject.Properties[$Service]
if ($null -eq $ServiceProperty) {
    $Available = @($Document.services.PSObject.Properties.Name) -join ", "
    throw "Unknown Salad service '$Service'. Available services: $Available"
}
$Definition = $ServiceProperty.Value

$StackProperty = $Document.PSObject.Properties["stack"]
$Stack = if ($null -ne $StackProperty) { $StackProperty.Value } else { $null }

function Get-ManifestString {
    param(
        [Parameter(Mandatory)][string]$Name,
        [object]$Primary,
        [object]$Fallback
    )

    $Value = ""
    if ($null -ne $Primary) {
        $Property = $Primary.PSObject.Properties[$Name]
        if ($null -ne $Property) {
            $Value = [string]$Property.Value
        }
    }
    if ([string]::IsNullOrWhiteSpace($Value) -and $null -ne $Fallback) {
        $Property = $Fallback.PSObject.Properties[$Name]
        if ($null -ne $Property) {
            $Value = [string]$Property.Value
        }
    }
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Salad manifest is missing '$Name' for service '$Service'."
    }
    return $Value.Trim()
}

$Organization = Get-ManifestString -Name "organization" -Primary $Stack -Fallback $Definition
$Project = Get-ManifestString -Name "project" -Primary $Stack -Fallback $Definition
$GroupName = Get-ManifestString -Name "group_name" -Primary $Definition -Fallback $null
$QueueName = Get-ManifestString -Name "queue_name" -Primary $Definition -Fallback $null
$Dockerfile = Join-Path $RepoRoot (Get-ManifestString -Name "dockerfile" -Primary $Definition -Fallback $null)
if ([string]::IsNullOrWhiteSpace($Image)) {
    $Image = Get-ManifestString -Name "image" -Primary $Definition -Fallback $null
}

$QueuePath = "/jobs"
$ContainerPort = 8080
$AutostartPolicy = $false
$RestartPolicy = "always"
if ($null -ne $Stack) {
    $Property = $Stack.PSObject.Properties["queue_path"]
    if ($null -ne $Property) {
        $QueuePath = [string]$Property.Value
    }
    $Property = $Stack.PSObject.Properties["container_port"]
    if ($null -ne $Property) {
        $ContainerPort = [int]$Property.Value
    }
    $Property = $Stack.PSObject.Properties["autostart_policy"]
    if ($null -ne $Property) {
        $AutostartPolicy = [bool]$Property.Value
    }
    $Property = $Stack.PSObject.Properties["restart_policy"]
    if ($null -ne $Property) {
        $RestartPolicy = [string]$Property.Value
    }
}

$OrganizationApiBase = "https://api.salad.com/api/public/organizations/$Organization"
$GpuClassesBase = "$OrganizationApiBase/gpu-classes"
$ApiBase = "$OrganizationApiBase/projects/$Project"
$ContainersBase = "$ApiBase/containers"
$QueuesBase = "$ApiBase/queues"
$SecretNames = @(
    "POSTGRES_DSN",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "HF_TOKEN",
    "SALAD_API_KEY"
)

function Get-RequiredEnvironmentNames {
    $Names = @()
    if ($null -ne $Stack) {
        $SharedProperty = $Stack.PSObject.Properties["shared_required_environment"]
        if ($null -ne $SharedProperty) {
            $Names += @($SharedProperty.Value | ForEach-Object { [string]$_ })
        }
    }

    $ServiceProperty = $Definition.PSObject.Properties["required_environment"]
    if ($null -ne $ServiceProperty) {
        $Names += @($ServiceProperty.Value | ForEach-Object { [string]$_ })
    }
    else {
        $LegacyProperty = $Definition.PSObject.Properties["required_secrets"]
        if ($null -ne $LegacyProperty) {
            $Names += @($LegacyProperty.Value | ForEach-Object { [string]$_ })
        }
    }

    return @($Names | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
}

function Assert-ServiceDefinition {
    if (-not (Test-Path -LiteralPath $Dockerfile -PathType Leaf)) {
        throw "Dockerfile for service '$Service' does not exist: $Dockerfile"
    }
    if ($Image -notmatch '^[^\s]+:[^\s/:]+$') {
        throw "Service '$Service' image must include an explicit mutable tag: $Image"
    }
    if ($QueuePath -ne "/jobs") {
        throw "Service '$Service' queue path must remain /jobs for the shared worker HTTP contract."
    }
    if ($ContainerPort -ne 8080) {
        throw "Service '$Service' container port must remain 8080 for the shared worker contract."
    }
    if ($RestartPolicy -notin @("always", "on_failure", "never")) {
        throw "Unsupported Salad restart policy '$RestartPolicy'."
    }

    $NamesProperty = $Definition.resources.PSObject.Properties["gpu_class_names"]
    if ($null -eq $NamesProperty -or @($NamesProperty.Value).Count -eq 0) {
        throw "Service '$Service' must declare at least one resources.gpu_class_names entry."
    }
    if ([int]$Definition.autoscaler.min_replicas -ne 0) {
        throw "Service '$Service' must keep min_replicas=0 so idle model workers scale to zero."
    }
    if ([int]$Definition.autoscaler.max_replicas -lt 1) {
        throw "Service '$Service' must allow at least one autoscaled replica."
    }
}

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

    if ($NonInteractive) {
        throw "$Name is missing and -NonInteractive was requested."
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
    $Value = $Value.Trim()
    [Environment]::SetEnvironmentVariable(
        $Name,
        $Value,
        [EnvironmentVariableTarget]::Process
    )
    return $Value
}

function Get-Headers {
    $ApiKey = Get-Setting -Name "SALAD_API_KEY" -Prompt "Salad API key" -Secret
    return @{
        "Salad-Api-Key" = $ApiKey
        "Accept" = "application/json"
        "User-Agent" = "ai-video-factory-worker-manager/2.0"
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

function Resolve-GpuClassIds {
    param([Parameter(Mandatory)][hashtable]$Headers)

    $NameProperty = $Definition.resources.PSObject.Properties["gpu_class_names"]
    $Names = @()
    if ($null -ne $NameProperty) {
        $Names = @($NameProperty.Value)
    }

    if ($Names.Count -eq 0) {
        $LegacyProperty = $Definition.resources.PSObject.Properties["gpu_classes"]
        if ($null -eq $LegacyProperty) {
            throw "Service '$Service' must define resources.gpu_class_names or gpu_classes."
        }
        $LegacyIds = @($LegacyProperty.Value | ForEach-Object { [string]$_ })
        if ($LegacyIds.Count -eq 0) {
            throw "Service '$Service' has no configured GPU classes."
        }
        Write-Warning "Service '$Service' still uses legacy GPU class UUIDs."
        return $LegacyIds
    }

    $Response = Invoke-RestMethod `
        -Uri $GpuClassesBase `
        -Headers $Headers `
        -TimeoutSec 30
    $Items = @()
    if ($Response.PSObject.Properties.Name -contains "items") {
        $Items = @($Response.items)
    }
    else {
        $Items = @($Response)
    }

    $Resolved = @()
    foreach ($ConfiguredName in $Names) {
        $Name = [string]$ConfiguredName
        $Matches = @($Items | Where-Object { [string]$_.name -eq $Name })
        if ($Matches.Count -eq 0) {
            $Available = @($Items | ForEach-Object { [string]$_.name }) -join ", "
            throw "Salad GPU class '$Name' was not found. Available classes: $Available"
        }
        if ($Matches.Count -gt 1) {
            throw "Salad returned multiple GPU classes named '$Name'."
        }
        $Resolved += [string]$Matches[0].id
    }

    Write-Host (
        "Resolved GPU profile for {0}: {1}" -f $Service, (@($Names) -join ", ")
    ) -ForegroundColor Green
    return $Resolved
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
        display_name = "$($Definition.display_name) Jobs"
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

function Try-Get-Group {
    param([Parameter(Mandatory)][hashtable]$Headers)

    try {
        return Invoke-RestMethod `
            -Uri "$ContainersBase/$GroupName" `
            -Headers $Headers `
            -TimeoutSec 30
    }
    catch {
        if ((Get-HttpStatusCode -ErrorRecord $_) -eq 404) {
            return $null
        }
        throw
    }
}

function Get-Group {
    param([Parameter(Mandatory)][hashtable]$Headers)

    $Group = Try-Get-Group -Headers $Headers
    if ($null -eq $Group) {
        throw "Container group '$GroupName' does not exist. Run -Action Prepare first."
    }
    return $Group
}

function Get-GroupStatus {
    param([Parameter(Mandatory)][object]$Group)

    $CurrentStateProperty = $Group.PSObject.Properties["current_state"]
    if ($null -eq $CurrentStateProperty -or $null -eq $CurrentStateProperty.Value) {
        return "unknown"
    }
    $StatusProperty = $CurrentStateProperty.Value.PSObject.Properties["status"]
    if ($null -eq $StatusProperty -or $null -eq $StatusProperty.Value) {
        return "unknown"
    }
    return [string]$StatusProperty.Value
}

function Get-GroupDescription {
    param([Parameter(Mandatory)][object]$Group)

    $CurrentStateProperty = $Group.PSObject.Properties["current_state"]
    if ($null -eq $CurrentStateProperty -or $null -eq $CurrentStateProperty.Value) {
        return ""
    }
    $DescriptionProperty = $CurrentStateProperty.Value.PSObject.Properties["description"]
    if ($null -eq $DescriptionProperty -or $null -eq $DescriptionProperty.Value) {
        return ""
    }
    return [string]$DescriptionProperty.Value
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
            (Get-GroupDescription -Group $Group)
        )
        if ($Status -eq $Expected -and -not $Group.pending_change) {
            return $Group
        }
    }
    while ((Get-Date) -lt $Deadline)

    throw "Container group did not reach status '$Expected' before timeout."
}

function Wait-ForGroupSettled {
    param(
        [Parameter(Mandatory)][hashtable]$Headers,
        [ValidateRange(1, 180)][int]$TimeoutMinutes
    )

    $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    do {
        Start-Sleep -Seconds 15
        $Group = Get-Group -Headers $Headers
        Write-Host (
            "{0} service={1} version={2} pending={3} status={4} description={5}" -f `
            (Get-Date -Format "HH:mm:ss"),
            $Service,
            $Group.version,
            $Group.pending_change,
            (Get-GroupStatus -Group $Group),
            (Get-GroupDescription -Group $Group)
        )
        if (-not $Group.pending_change) {
            return $Group
        }
    }
    while ((Get-Date) -lt $Deadline)

    throw "Salad did not finish preparing service '$Service' before the timeout."
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
            port = $ContainerPort
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

    foreach ($Name in Get-RequiredEnvironmentNames) {
        $IsSecret = $SecretNames -contains [string]$Name
        $Environment[[string]$Name] = Get-Setting `
            -Name ([string]$Name) `
            -Prompt ([string]$Name) `
            -Secret:$IsSecret
    }
    return $Environment
}

function New-ContainerConfiguration {
    param(
        [Parameter(Mandatory)][string]$PinnedImage,
        [Parameter(Mandatory)][hashtable]$WorkerEnvironment,
        [Parameter(Mandatory)][string[]]$GpuClassIds,
        [switch]$IncludePriority
    )

    $Container = @{
        image = $PinnedImage
        resources = @{
            cpu = [int]$Definition.resources.cpu
            memory = [int]$Definition.resources.memory
            gpu_classes = $GpuClassIds
            shm_size = [int]$Definition.resources.shm_size
            storage_amount = [Int64]$Definition.resources.storage_amount
        }
        environment_variables = $WorkerEnvironment
        image_caching = $true
    }
    if ($IncludePriority) {
        $Container["priority"] = [string]$Definition.priority
    }
    return $Container
}

function New-QueueAutoscalerConfiguration {
    return @{
        min_replicas = [int]$Definition.autoscaler.min_replicas
        max_replicas = [int]$Definition.autoscaler.max_replicas
        desired_queue_length = [int]$Definition.autoscaler.desired_queue_length
        polling_period = [int]$Definition.autoscaler.polling_period
        max_upscale_per_minute = [int]$Definition.autoscaler.max_upscale_per_minute
        max_downscale_per_minute = [int]$Definition.autoscaler.max_downscale_per_minute
    }
}

function New-QueueConnectionConfiguration {
    return @{
        path = $QueuePath
        port = $ContainerPort
        queue_name = $QueueName
    }
}

function New-ContainerGroup {
    param(
        [Parameter(Mandatory)][hashtable]$Headers,
        [Parameter(Mandatory)][string]$PinnedImage,
        [Parameter(Mandatory)][hashtable]$WorkerEnvironment,
        [Parameter(Mandatory)][string[]]$GpuClassIds
    )

    $CreateBody = @{
        name = $GroupName
        display_name = [string]$Definition.display_name
        autostart_policy = $AutostartPolicy
        replicas = 0
        restart_policy = $RestartPolicy
        scheduled_scaling_enabled = $false
        container = New-ContainerConfiguration `
            -PinnedImage $PinnedImage `
            -WorkerEnvironment $WorkerEnvironment `
            -GpuClassIds $GpuClassIds `
            -IncludePriority
        startup_probe = New-Probe -Probe $Definition.probes.startup
        readiness_probe = New-Probe -Probe $Definition.probes.readiness
        liveness_probe = New-Probe -Probe $Definition.probes.liveness
        queue_connection = New-QueueConnectionConfiguration
        queue_autoscaler = New-QueueAutoscalerConfiguration
    } | ConvertTo-Json -Depth 20

    Write-Host "Creating container group: $GroupName" -ForegroundColor Cyan
    Invoke-RestMethod `
        -Method Post `
        -Uri $ContainersBase `
        -Headers $Headers `
        -ContentType "application/json" `
        -Body $CreateBody `
        -TimeoutSec 60 |
        Out-Null
}

function Update-ContainerGroup {
    param(
        [Parameter(Mandatory)][hashtable]$Headers,
        [Parameter(Mandatory)][string]$PinnedImage,
        [Parameter(Mandatory)][hashtable]$WorkerEnvironment,
        [Parameter(Mandatory)][string[]]$GpuClassIds
    )

    $PatchBody = @{
        replicas = 0
        container = New-ContainerConfiguration `
            -PinnedImage $PinnedImage `
            -WorkerEnvironment $WorkerEnvironment `
            -GpuClassIds $GpuClassIds `
            -IncludePriority
        startup_probe = New-Probe -Probe $Definition.probes.startup
        readiness_probe = New-Probe -Probe $Definition.probes.readiness
        liveness_probe = New-Probe -Probe $Definition.probes.liveness
        queue_connection = New-QueueConnectionConfiguration
        queue_autoscaler = New-QueueAutoscalerConfiguration
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
}

function Ensure-PreparedZeroReplicas {
    param(
        [Parameter(Mandatory)][hashtable]$Headers,
        [Parameter(Mandatory)][object]$Group
    )

    if ([int]$Group.replicas -eq 0) {
        return $Group
    }

    Write-Warning (
        "Salad raised '$GroupName' to replicas=$([int]$Group.replicas) while preparing $Service. " +
        "Forcing replicas back to zero before Prepare completes."
    )
    $Body = @{ replicas = 0 } | ConvertTo-Json
    Invoke-RestMethod `
        -Method Patch `
        -Uri "$ContainersBase/$GroupName" `
        -Headers $Headers `
        -ContentType "application/merge-patch+json" `
        -Body $Body `
        -TimeoutSec 60 |
        Out-Null

    $Updated = Wait-ForGroupSettled `
        -Headers $Headers `
        -TimeoutMinutes $PrepareTimeoutMinutes
    if ([int]$Updated.replicas -ne 0) {
        throw (
            "Prepared service '$Service' could not be normalized to zero replicas; " +
            "current replicas=$([int]$Updated.replicas)."
        )
    }
    return $Updated
}

function Assert-PreparedGroup {
    param(
        [Parameter(Mandatory)][object]$Group,
        [Parameter(Mandatory)][string]$PinnedImage
    )

    if ($Group.container.image -ne $PinnedImage) {
        throw "Salad did not activate the expected image. Received: $($Group.container.image)"
    }
    if ([string]$Group.priority -ne [string]$Definition.priority) {
        throw (
            "Salad did not activate the expected priority '$([string]$Definition.priority)'. " +
            "Received: $([string]$Group.priority)"
        )
    }
    if ([string]$Group.queue_connection.queue_name -ne $QueueName) {
        throw "Salad did not activate the expected queue: $QueueName"
    }
    if ([int]$Group.replicas -ne 0) {
        throw "Prepared service '$Service' must remain at zero replicas."
    }
}

function Show-Status {
    param([Parameter(Mandatory)][hashtable]$Headers)

    $Group = Try-Get-Group -Headers $Headers
    if ($null -eq $Group) {
        Write-Host (
            "service={0} group={1} queue={2} status=missing" -f `
            $Service,
            $GroupName,
            $QueueName
        ) -ForegroundColor Yellow
        return
    }

    $ConfiguredGpuProperty = $Definition.resources.PSObject.Properties["gpu_class_names"]
    $ConfiguredGpu = "legacy UUIDs"
    if ($null -ne $ConfiguredGpuProperty) {
        $ConfiguredGpu = @($ConfiguredGpuProperty.Value) -join ","
    }

    $Group |
        Select-Object `
            name,
            version,
            replicas,
            priority,
            pending_change,
            @{Name = "Service"; Expression = {$Service}},
            @{Name = "Queue"; Expression = {$QueueName}},
            @{Name = "RequestedGPU"; Expression = {$ConfiguredGpu}},
            @{Name = "Status"; Expression = {Get-GroupStatus -Group $_}},
            @{Name = "Description"; Expression = {Get-GroupDescription -Group $_}},
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
Assert-ServiceDefinition

if ($Action -eq "Validate") {
    $Required = Get-RequiredEnvironmentNames
    Write-Host (
        "VALID service={0} group={1} queue={2} gpu={3} required_env={4}" -f `
        $Service,
        $GroupName,
        $QueueName,
        (@($Definition.resources.gpu_class_names) -join ","),
        ($Required -join ",")
    ) -ForegroundColor Green
    exit 0
}

$Headers = Get-Headers

switch ($Action) {
    "Status" {
        Show-Status -Headers $Headers
        exit 0
    }

    "Stop" {
        $Group = Try-Get-Group -Headers $Headers
        if ($null -eq $Group) {
            Write-Host "$Service worker group does not exist; nothing to stop." -ForegroundColor Green
            exit 0
        }
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
        Write-Host (
            "Container group is running with autoscaler min=0; replicas start only when jobs queue."
        ) -ForegroundColor Cyan
        Show-Status -Headers $Headers
        exit 0
    }

    "Prepare" {
        Ensure-Queue -Headers $Headers
        $ExistingGroup = Try-Get-Group -Headers $Headers
        if ($null -ne $ExistingGroup -and (Get-GroupStatus -Group $ExistingGroup) -ne "stopped") {
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
        $GpuClassIds = @(Resolve-GpuClassIds -Headers $Headers)

        if ($null -eq $ExistingGroup) {
            New-ContainerGroup `
                -Headers $Headers `
                -PinnedImage $PinnedImage `
                -WorkerEnvironment $WorkerEnvironment `
                -GpuClassIds $GpuClassIds
            $Group = Wait-ForGroupSettled `
                -Headers $Headers `
                -TimeoutMinutes $PrepareTimeoutMinutes
        }
        else {
            $PreviousVersion = [int]$ExistingGroup.version
            Update-ContainerGroup `
                -Headers $Headers `
                -PinnedImage $PinnedImage `
                -WorkerEnvironment $WorkerEnvironment `
                -GpuClassIds $GpuClassIds
            $Group = Wait-ForGroupSettled `
                -Headers $Headers `
                -TimeoutMinutes $PrepareTimeoutMinutes
            if ([int]$Group.version -le $PreviousVersion) {
                throw "Container group version did not increase."
            }
        }

        $Group = Ensure-PreparedZeroReplicas -Headers $Headers -Group $Group
        Assert-PreparedGroup -Group $Group -PinnedImage $PinnedImage
        $WorkerEnvironment = $null
        $GpuClassIds = $null

        Write-Host "$Service worker image/config prepared; group remains stopped at zero replicas." `
            -ForegroundColor Green
        Show-Status -Headers $Headers
    }
}
