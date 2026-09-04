[CmdletBinding()]
param(
    [ValidateRange(1, 30)]
    [int]$TimeoutMinutes = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Organization = "yagobordellorg"
$Project = "aivideofactory"
$GroupName = "ai-video-factory-worker"
$ContainersBase = (
    "https://api.salad.com/api/public/organizations/" +
    "$Organization/projects/$Project/containers"
)
$GroupUri = "$ContainersBase/$GroupName"

function Get-SecretSetting {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Prompt
    )

    $Value = [Environment]::GetEnvironmentVariable(
        $Name,
        [EnvironmentVariableTarget]::Process
    )
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value
    }

    $SecureValue = Read-Host $Prompt -AsSecureString
    $Credential = [PSCredential]::new("phase8", $SecureValue)
    $Value = $Credential.GetNetworkCredential().Password
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is empty."
    }
    return $Value.Trim()
}

$SaladApiKey = Get-SecretSetting -Name "SALAD_API_KEY" -Prompt "Salad API key"
$Headers = @{
    "Salad-Api-Key" = $SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-phase8/1.0"
}

$Group = Invoke-RestMethod -Uri $GroupUri -Headers $Headers -TimeoutSec 30
$Status = [string]$Group.current_state.status

if ($Status -eq "stopped") {
    Invoke-RestMethod `
        -Method Post `
        -Uri "$GroupUri/start" `
        -Headers $Headers `
        -TimeoutSec 60 |
        Out-Null
}
else {
    Write-Host "Container group is already enabled with status=$Status." -ForegroundColor Green
}

$Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    Start-Sleep -Seconds 5
    $Group = Invoke-RestMethod -Uri $GroupUri -Headers $Headers -TimeoutSec 30
    $Status = [string]$Group.current_state.status
    Write-Host (
        "{0} status={1} replicas={2} pending={3}" -f `
        (Get-Date -Format "HH:mm:ss"),
        $Status,
        $Group.replicas,
        $Group.pending_change
    )

    if ($Status -ne "stopped" -and -not $Group.pending_change) {
        Write-Host (
            "Phase 8 worker group is enabled. With min_replicas=0, queue demand creates the GPU replica."
        ) -ForegroundColor Green
        $SaladApiKey = $null
        exit 0
    }
}
while ((Get-Date) -lt $Deadline)

throw "Container group did not become enabled before the timeout."
