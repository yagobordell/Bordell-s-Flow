[CmdletBinding()]
param(
    [ValidateSet("Validate", "Prepare", "Start", "Status", "Stop")]
    [string]$Action = "Status",

    [string[]]$Services = @(),

    [string]$EnvFile = ".env",

    [ValidateRange(10, 180)]
    [int]$PrepareTimeoutMinutes = 120,

    [switch]$SkipBuild,

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"
$WorkerManager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
$QueueAttachmentRepair = Join-Path $PSScriptRoot "repair_salad_queue_attachment.ps1"
$ScaleToZeroStarter = Join-Path $PSScriptRoot "start_salad_scale_to_zero.ps1"
$ZeroReplicaGuard = Join-Path $PSScriptRoot "ensure_salad_zero_replicas.ps1"

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
        $Credential = [PSCredential]::new("salad-stack", $SecureValue)
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

function Get-ServiceRequiredEnvironment {
    param([Parameter(Mandatory)][object]$Definition)

    $Names = @(
        $Document.stack.shared_required_environment |
            ForEach-Object { [string]$_ }
    )
    $RequiredProperty = $Definition.PSObject.Properties["required_environment"]
    if ($null -ne $RequiredProperty) {
        $Names += @($RequiredProperty.Value | ForEach-Object { [string]$_ })
    }
    else {
        $LegacyProperty = $Definition.PSObject.Properties["required_secrets"]
        if ($null -ne $LegacyProperty) {
            $Names += @($LegacyProperty.Value | ForEach-Object { [string]$_ })
        }
    }
    return @($Names | Select-Object -Unique)
}

function Assert-StackManifest {
    if ([string]$Document.schema_version -ne "2") {
        throw "deploy/salad/services.json must use schema_version=2."
    }
    if ([string]::IsNullOrWhiteSpace([string]$Document.stack.organization)) {
        throw "Salad stack organization is missing."
    }
    if ([string]::IsNullOrWhiteSpace([string]$Document.stack.project)) {
        throw "Salad stack project is missing."
    }

    $Order = @($Document.stack.service_order | ForEach-Object { [string]$_ })
    if ($Order.Count -eq 0 -or $Order.Count -ne @($Order | Select-Object -Unique).Count) {
        throw "Salad stack service_order must contain unique service names."
    }

    $Known = @($Document.services.PSObject.Properties.Name)
    foreach ($Name in $Order) {
        if ($Known -notcontains $Name) {
            throw "service_order references unknown service '$Name'."
        }
    }
    foreach ($Name in $Known) {
        if ($Order -notcontains $Name) {
            throw "Service '$Name' is missing from stack.service_order."
        }
    }

    $Groups = @()
    $Queues = @()
    foreach ($Property in $Document.services.PSObject.Properties) {
        $Definition = $Property.Value
        $Groups += [string]$Definition.group_name
        $Queues += [string]$Definition.queue_name
        if ([int]$Definition.autoscaler.min_replicas -ne 0) {
            throw "Service '$($Property.Name)' must keep min_replicas=0."
        }
        if ([int]$Definition.autoscaler.max_replicas -lt 1) {
            throw "Service '$($Property.Name)' must allow at least one replica."
        }
        $Dockerfile = Join-Path $RepoRoot ([string]$Definition.dockerfile)
        if (-not (Test-Path -LiteralPath $Dockerfile -PathType Leaf)) {
            throw "Service '$($Property.Name)' Dockerfile is missing: $Dockerfile"
        }
        if ([string]$Definition.image -notmatch '^[^\s]+:[^\s/:]+$') {
            throw "Service '$($Property.Name)' image must contain an explicit tag."
        }
    }

    if ($Groups.Count -ne @($Groups | Select-Object -Unique).Count) {
        throw "Every model service must have its own Salad container group."
    }
    if ($Queues.Count -ne @($Queues | Select-Object -Unique).Count) {
        throw "Every model service must have its own Salad job queue."
    }
}

function Invoke-QueueRepair {
    param(
        [Parameter(Mandatory)][string]$Name,
        [switch]$AllowMissing
    )

    $RepairArguments = @{
        Service = $Name
        EnvFile = $EnvFile
    }
    if ($AllowMissing) {
        $RepairArguments["AllowMissing"] = $true
    }
    if ($NonInteractive) {
        $RepairArguments["NonInteractive"] = $true
    }

    & $QueueAttachmentRepair @RepairArguments
    $Succeeded = $?
    if (-not $Succeeded) {
        throw "Salad Job Queue attachment repair failed for service '$Name'."
    }
}

function Invoke-WorkerAction {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$WorkerAction
    )

    $WorkerArguments = @{
        Service = $Name
        Action = $WorkerAction
        EnvFile = $EnvFile
        PrepareTimeoutMinutes = $PrepareTimeoutMinutes
    }
    if ($SkipBuild) {
        $WorkerArguments["SkipBuild"] = $true
    }
    if ($NonInteractive) {
        $WorkerArguments["NonInteractive"] = $true
    }

    & $WorkerManager @WorkerArguments
    $Succeeded = $?
    if (-not $Succeeded) {
        throw "Salad $WorkerAction failed for service '$Name'."
    }
}

function Invoke-ScaleToZeroStart {
    param([Parameter(Mandatory)][string]$Name)

    $Arguments = @{
        Service = $Name
        EnvFile = $EnvFile
    }
    if ($NonInteractive) {
        $Arguments["NonInteractive"] = $true
    }

    & $ScaleToZeroStarter @Arguments
    $Succeeded = $?
    if (-not $Succeeded) {
        throw "Salad scale-to-zero Start failed for service '$Name'."
    }
}

function Invoke-ZeroReplicaGuard {
    param([Parameter(Mandatory)][string]$Name)

    $Arguments = @{
        Service = $Name
        EnvFile = $EnvFile
    }
    if ($NonInteractive) {
        $Arguments["NonInteractive"] = $true
    }

    & $ZeroReplicaGuard @Arguments
    $Succeeded = $?
    if (-not $Succeeded) {
        throw "Salad zero-replica guard failed for service '$Name'."
    }
}

foreach ($RequiredPath in @($ManifestPath, $WorkerManager)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required Salad deployment file not found: $RequiredPath"
    }
}
if ($Action -eq "Prepare" -and -not (Test-Path -LiteralPath $QueueAttachmentRepair -PathType Leaf)) {
    throw "Salad queue attachment repair helper not found: $QueueAttachmentRepair"
}
if ($Action -eq "Start" -and -not (Test-Path -LiteralPath $ScaleToZeroStarter -PathType Leaf)) {
    throw "Salad scale-to-zero starter not found: $ScaleToZeroStarter"
}
if ($Action -eq "Stop" -and -not (Test-Path -LiteralPath $ZeroReplicaGuard -PathType Leaf)) {
    throw "Salad zero-replica guard not found: $ZeroReplicaGuard"
}

Import-EnvFile -Path $EnvFile
$Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
Assert-StackManifest

$ConfiguredOrder = @($Document.stack.service_order | ForEach-Object { [string]$_ })
if ($Services.Count -eq 0) {
    $Selected = $ConfiguredOrder
}
else {
    $Selected = @()
    foreach ($Name in $Services) {
        if ($ConfiguredOrder -notcontains $Name) {
            throw "Unknown Salad service '$Name'. Available: $($ConfiguredOrder -join ', ')"
        }
        if ($Selected -notcontains $Name) {
            $Selected += $Name
        }
    }
}

if ($Action -eq "Validate") {
    foreach ($Name in $Selected) {
        Write-Host "=== Salad Validate : $Name ===" -ForegroundColor Cyan
        Invoke-WorkerAction -Name $Name -WorkerAction "Validate"
    }
    Write-Host (
        "VALID stack={0}/{1} services={2}" -f `
        $Document.stack.organization,
        $Document.stack.project,
        ($Selected -join ",")
    ) -ForegroundColor Green
    return
}

Get-Setting -Name "SALAD_API_KEY" -Prompt "Salad API key" -Secret | Out-Null
if ($Action -eq "Prepare") {
    $SecretNames = @(
        "POSTGRES_DSN",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "HF_TOKEN"
    )
    $Required = @()
    foreach ($Name in $Selected) {
        $Definition = $Document.services.PSObject.Properties[$Name].Value
        $Required += Get-ServiceRequiredEnvironment -Definition $Definition
    }
    foreach ($Name in @($Required | Select-Object -Unique)) {
        Get-Setting `
            -Name $Name `
            -Prompt $Name `
            -Secret:($SecretNames -contains $Name) |
            Out-Null
    }
}

$ExecutionOrder = $Selected
if ($Action -eq "Stop") {
    $ExecutionOrder = @($Selected)
    [array]::Reverse($ExecutionOrder)
}

$StopFailures = @()

foreach ($Name in $ExecutionOrder) {
    Write-Host "=== Salad $Action : $Name ===" -ForegroundColor Cyan

    switch ($Action) {
        "Prepare" {
            Invoke-QueueRepair -Name $Name -AllowMissing
            Invoke-WorkerAction -Name $Name -WorkerAction "Prepare"
            Invoke-QueueRepair -Name $Name
        }
        "Start" {
            Invoke-ScaleToZeroStart -Name $Name
        }
        "Status" {
            Invoke-WorkerAction -Name $Name -WorkerAction "Status"
        }
        "Stop" {
            $StopError = $null
            try {
                Invoke-WorkerAction -Name $Name -WorkerAction "Stop"
            }
            catch {
                $StopError = $_
                Write-Warning (
                    "Salad Stop request failed for '$Name'; the zero-replica guard will still " +
                    "verify the terminal state. Error: $($_.Exception.Message)"
                )
            }

            try {
                Invoke-ZeroReplicaGuard -Name $Name
                if ($null -ne $StopError) {
                    Write-Warning (
                        "Salad Stop request for '$Name' reported an error, but the zero-replica " +
                        "guard confirmed stopped/replicas=0; treating cleanup as successful."
                    )
                }
            }
            catch {
                $StopFailures += $_
                if ($null -ne $StopError) {
                    Write-Warning (
                        "Salad Stop and zero-replica guard both failed for '$Name'. " +
                        "Stop error: $($StopError.Exception.Message). " +
                        "Guard error: $($_.Exception.Message)"
                    )
                }
                else {
                    Write-Warning (
                        "Salad zero-replica guard failed for '$Name': $($_.Exception.Message)"
                    )
                }
            }
        }
        default {
            throw "Unsupported Salad stack action '$Action'."
        }
    }
}

if ($Action -eq "Stop" -and $StopFailures.Count -gt 0) {
    throw (
        "Salad stack Stop could not verify terminal zero-replica state for " +
        "$($StopFailures.Count) service(s). First error: " +
        "$($StopFailures[0].Exception.Message)"
    )
}

Write-Host (
    "Salad stack action complete: action={0} services={1}" -f `
    $Action,
    ($ExecutionOrder -join ",")
) -ForegroundColor Green