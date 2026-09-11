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

    $Names = @($Document.stack.shared_required_environment | ForEach-Object { [string]$_ })
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
    $SchemaProperty = $Document.PSObject.Properties["schema_version"]
    if ($null -eq $SchemaProperty -or [string]$SchemaProperty.Value -ne "2") {
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

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Salad stack manifest not found: $ManifestPath"
}
if (-not (Test-Path -LiteralPath $WorkerManager -PathType Leaf)) {
    throw "Salad worker manager not found: $WorkerManager"
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
        & $WorkerManager `
            -Service $Name `
            -Action Validate `
            -EnvFile $EnvFile `
            -NonInteractive:$NonInteractive
        $CallSucceeded = $?
        if (-not $CallSucceeded) {
            throw "Validation failed for Salad service '$Name'."
        }
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
    $SecretNames = @("POSTGRES_DSN", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "HF_TOKEN")
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

foreach ($Name in $ExecutionOrder) {
    Write-Host "=== Salad $Action : $Name ===" -ForegroundColor Cyan
    $WorkerArguments = @{
        Service = $Name
        Action = $Action
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
    $CallSucceeded = $?
    if (-not $CallSucceeded) {
        throw "Salad $Action failed for service '$Name'."
    }
}

Write-Host (
    "Salad stack action complete: action={0} services={1}" -f $Action, ($ExecutionOrder -join ",")
) -ForegroundColor Green
