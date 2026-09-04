[CmdletBinding()]
param(
    [ValidateRange(1, 120)]
    [int]$Samples = 1,

    [ValidateRange(1, 300)]
    [int]$IntervalSeconds = 30
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

for ($Sample = 1; $Sample -le $Samples; $Sample++) {
    $Group = Invoke-RestMethod -Uri $GroupUri -Headers $Headers -TimeoutSec 30
    $Response = Invoke-RestMethod `
        -Uri "$GroupUri/instances" `
        -Headers $Headers `
        -TimeoutSec 30

    $InstanceRows = @()
    if ($Response.PSObject.Properties.Name -contains "instances") {
        $InstanceRows = @($Response.instances)
    }
    elseif ($Response.PSObject.Properties.Name -contains "items") {
        $InstanceRows = @($Response.items)
    }

    Write-Host ""
    Write-Host (
        "{0} group_status={1} replicas={2} pending={3} instances={4}" -f `
        (Get-Date -Format "HH:mm:ss"),
        [string]$Group.current_state.status,
        $Group.replicas,
        $Group.pending_change,
        $InstanceRows.Count
    )

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

    if ($Sample -lt $Samples) {
        Start-Sleep -Seconds $IntervalSeconds
    }
}

$SaladApiKey = $null
