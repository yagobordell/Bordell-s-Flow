[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("whisper", "breeze_tts2", "fish_speech", "ideogram4", "ltx25", "realesrgan")]
    [string]$Service,

    [string]$EnvFile = ".env",

    [ValidateRange(1, 10)]
    [int]$TimeoutMinutes = 3,

    [ValidateSet("Manifest", "WarmScaleOut")]
    [string]$Mode = "Manifest",

    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ManifestPath = Join-Path $RepoRoot "deploy\salad\services.json"

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

function Get-SaladApiKey {
    $Value = [Environment]::GetEnvironmentVariable(
        "SALAD_API_KEY",
        [EnvironmentVariableTarget]::Process
    )
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value.Trim()
    }
    if ($NonInteractive) {
        throw "SALAD_API_KEY is missing and -NonInteractive was requested."
    }

    $SecureValue = Read-Host "Salad API key" -AsSecureString
    $Credential = [PSCredential]::new("salad-scale-to-zero-restore", $SecureValue)
    $Value = $Credential.GetNetworkCredential().Password
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "SALAD_API_KEY is empty."
    }
    return $Value.Trim()
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

function Test-TransientSaladFailure {
    param([Parameter(Mandatory)][object]$ErrorRecord)

    $StatusCode = Get-HttpStatusCode -ErrorRecord $ErrorRecord
    if ($StatusCode -in @(408, 429, 500, 502, 503, 504)) {
        return $true
    }
    $Message = [string]$ErrorRecord.Exception.Message
    return $Message -match (
        "(?i)timed out|timeout|upstream connect error|disconnect/reset|" +
        "remote connection failure|server unavailable|gateway timeout"
    )
}

function Invoke-SaladRequest {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Operation,
        [string]$Method = "Get",
        [string]$ContentType = "",
        [string]$Body = "",
        [ValidateRange(1, 120)][int]$TimeoutSec = 30,
        [ValidateRange(1, 10)][int]$MaxAttempts = 6
    )

    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt += 1) {
        try {
            $Arguments = @{
                Method = $Method
                Uri = $Uri
                Headers = $Headers
                TimeoutSec = $TimeoutSec
            }
            if (-not [string]::IsNullOrWhiteSpace($ContentType)) {
                $Arguments["ContentType"] = $ContentType
            }
            if (-not [string]::IsNullOrWhiteSpace($Body)) {
                $Arguments["Body"] = $Body
            }
            return Invoke-RestMethod @Arguments
        }
        catch {
            if (-not (Test-TransientSaladFailure -ErrorRecord $_) -or $Attempt -ge $MaxAttempts) {
                throw
            }
            $DelaySeconds = [Math]::Min(15, 2 * $Attempt)
            Write-Warning (
                "$Service scale-to-zero restore operation '$Operation' failed transiently " +
                "(attempt $Attempt/$MaxAttempts): $($_.Exception.Message). " +
                "Retrying in ${DelaySeconds}s."
            )
            Start-Sleep -Seconds $DelaySeconds
        }
    }

    throw "Unreachable Salad retry state for '$Operation'."
}

function Try-Get-Group {
    try {
        return Invoke-SaladRequest -Uri $GroupUrl -Operation "read container group"
    }
    catch {
        if ((Get-HttpStatusCode -ErrorRecord $_) -eq 404) {
            return $null
        }
        throw
    }
}

function New-TargetAutoscaler {
    $MinReplicas = if ($Mode -eq "WarmScaleOut") {
        1
    }
    else {
        [int]$Definition.autoscaler.min_replicas
    }
    return @{
        min_replicas = $MinReplicas
        max_replicas = [int]$Definition.autoscaler.max_replicas
        desired_queue_length = [int]$Definition.autoscaler.desired_queue_length
        polling_period = [int]$Definition.autoscaler.polling_period
        max_upscale_per_minute = [int]$Definition.autoscaler.max_upscale_per_minute
        max_downscale_per_minute = [int]$Definition.autoscaler.max_downscale_per_minute
    }
}

function Get-RemoteQueueAutoscaler {
    param([Parameter(Mandatory)][object]$Group)

    $Property = $Group.PSObject.Properties["queue_autoscaler"]
    if ($null -eq $Property -or $null -eq $Property.Value) {
        return $null
    }
    return $Property.Value
}

function Test-TargetAutoscaler {
    param([Parameter(Mandatory)][object]$Group)

    $Autoscaler = Get-RemoteQueueAutoscaler -Group $Group
    if ($null -eq $Autoscaler) {
        return $false
    }
    $ExpectedMinReplicas = if ($Mode -eq "WarmScaleOut") {
        1
    }
    else {
        [int]$Definition.autoscaler.min_replicas
    }

    return (
        [int]$Autoscaler.min_replicas -eq $ExpectedMinReplicas -and
        [int]$Autoscaler.max_replicas -eq [int]$Definition.autoscaler.max_replicas -and
        [int]$Autoscaler.desired_queue_length -eq [int]$Definition.autoscaler.desired_queue_length -and
        [int]$Autoscaler.polling_period -eq [int]$Definition.autoscaler.polling_period -and
        [int]$Autoscaler.max_upscale_per_minute -eq `
            [int]$Definition.autoscaler.max_upscale_per_minute -and
        [int]$Autoscaler.max_downscale_per_minute -eq `
            [int]$Definition.autoscaler.max_downscale_per_minute
    )
}

Import-EnvFile -Path $EnvFile
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Salad stack manifest not found: $ManifestPath"
}

$Document = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$ServiceProperty = $Document.services.PSObject.Properties[$Service]
if ($null -eq $ServiceProperty) {
    throw "Unknown Salad service '$Service'."
}
$Definition = $ServiceProperty.Value
$Organization = [string]$Document.stack.organization
$Project = [string]$Document.stack.project
$GroupName = [string]$Definition.group_name
$GroupUrl = (
    "https://api.salad.com/api/public/organizations/{0}/projects/{1}/containers/{2}" -f
    $Organization,
    $Project,
    $GroupName
)
$Headers = @{
    "Salad-Api-Key" = Get-SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-scale-to-zero-restore/1.0"
}

$Group = Try-Get-Group
if ($null -eq $Group) {
    if ($Mode -eq "WarmScaleOut") {
        throw "$Service worker group does not exist; cannot arm warm scale-out."
    }
    Write-Host "$Service worker group does not exist; scale-to-zero restore not needed." `
        -ForegroundColor Green
    exit 0
}
if ($Mode -eq "WarmScaleOut") {
    if ($Service -ne "ltx25") {
        throw "WarmScaleOut is currently reserved for the ltx25 production lifecycle."
    }
    $Status = [string]$Group.current_state.status
    if (
        [bool]$Group.pending_change -or
        [int]$Group.replicas -lt 1 -or
        $Status -notin @("deploying", "running")
    ) {
        throw (
            "WarmScaleOut requires an active held LTX group; " +
            "status=$Status replicas=$([int]$Group.replicas) " +
            "pending=$([bool]$Group.pending_change)."
        )
    }
}
$RemoteAutoscaler = Get-RemoteQueueAutoscaler -Group $Group
if ($null -eq $RemoteAutoscaler) {
    Write-Host (
        "$Service API response omits queue_autoscaler; skipping legacy autoscaler restore. " +
        "The Stop path and zero-replica guard remain authoritative for cleanup."
    ) -ForegroundColor Yellow
    exit 0
}
if (Test-TargetAutoscaler -Group $Group) {
    $TargetDescription = if ($Mode -eq "WarmScaleOut") {
        "warm scale-out min_replicas=1/max_replicas=$([int]$Definition.autoscaler.max_replicas)"
    }
    else {
        "scale-to-zero manifest"
    }
    Write-Host "$Service queue autoscaler already matches $TargetDescription." `
        -ForegroundColor Green
    exit 0
}

$TargetMinReplicas = if ($Mode -eq "WarmScaleOut") {
    1
}
else {
    [int]$Definition.autoscaler.min_replicas
}
Write-Host (
    "$Service applying autoscaler mode=$Mode min_replicas=$TargetMinReplicas " +
    "max_replicas=$([int]$Definition.autoscaler.max_replicas)."
) -ForegroundColor Cyan
$Body = @{ queue_autoscaler = New-TargetAutoscaler } | ConvertTo-Json -Depth 10
Invoke-SaladRequest `
    -Method "Patch" `
    -Uri $GroupUrl `
    -Operation "restore manifest autoscaler" `
    -ContentType "application/merge-patch+json" `
    -Body $Body `
    -TimeoutSec 60 |
    Out-Null

$Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    Start-Sleep -Seconds 5
    $Group = Try-Get-Group
    if ($null -eq $Group) {
        throw "Container group '$GroupName' disappeared while restoring scale-to-zero autoscaling."
    }
    if (-not [bool]$Group.pending_change -and (Test-TargetAutoscaler -Group $Group)) {
        $Completion = if ($Mode -eq "WarmScaleOut") {
            "warm scale-out autoscaler armed"
        }
        else {
            "scale-to-zero autoscaler restored"
        }
        Write-Host "$Service $Completion." -ForegroundColor Green
        exit 0
    }
}
while ((Get-Date) -lt $Deadline)

throw "Container group '$GroupName' did not apply autoscaler mode '$Mode' before timeout."
