[CmdletBinding()]
param(
    [ValidateSet("Verify", "Apply")]
    [string]$Action = "Verify"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Organization = "yagobordellorg"
$Project = "aivideofactory"
$GroupName = "ai-video-factory-worker"
$ModelRepository = "Lightricks/LTX-2.5"
$ContainersBase = (
    "https://api.salad.com/api/public/organizations/" +
    "$Organization/projects/$Project/containers"
)

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

function Assert-HuggingFaceAccess {
    param([Parameter(Mandatory)][string]$Token)

    $Headers = @{
        "Authorization" = "Bearer $Token"
        "Accept" = "application/octet-stream"
        "User-Agent" = "ai-video-factory-phase8/1.0"
    }
    $Uri = "https://huggingface.co/$ModelRepository/resolve/main/.gitattributes"
    $Destination = Join-Path ([IO.Path]::GetTempPath()) "ltx25-gated-access-check"
    try {
        Invoke-WebRequest `
            -Uri $Uri `
            -Headers $Headers `
            -OutFile $Destination `
            -MaximumRedirection 5 `
            -TimeoutSec 30 |
            Out-Null
        if (-not (Test-Path $Destination) -or (Get-Item $Destination).Length -lt 1) {
            throw "Hugging Face access check returned an empty file."
        }
    }
    finally {
        Remove-Item $Destination -Force -ErrorAction SilentlyContinue
    }

    Write-Host "Hugging Face gated access verified for $ModelRepository." -ForegroundColor Green
}

$HfToken = Get-SecretSetting -Name "HF_TOKEN" -Prompt "Hugging Face read token"
Assert-HuggingFaceAccess -Token $HfToken

if ($Action -eq "Verify") {
    exit 0
}

$SaladApiKey = Get-SecretSetting -Name "SALAD_API_KEY" -Prompt "Salad API key"
$SaladHeaders = @{
    "Salad-Api-Key" = $SaladApiKey
    "Accept" = "application/json"
    "User-Agent" = "ai-video-factory-phase8/1.0"
}

$Group = Invoke-RestMethod `
    -Uri "$ContainersBase/$GroupName" `
    -Headers $SaladHeaders `
    -TimeoutSec 30

if ([string]$Group.current_state.status -ne "stopped") {
    throw "Container group must be stopped before applying HF_TOKEN."
}

$EnvironmentVariables = @{}
foreach ($Property in $Group.container.environment_variables.PSObject.Properties) {
    $EnvironmentVariables[$Property.Name] = [string]$Property.Value
}
$EnvironmentVariables["HF_TOKEN"] = $HfToken

$PatchBody = @{
    container = @{
        environment_variables = $EnvironmentVariables
    }
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
    -Method Patch `
    -Uri "$ContainersBase/$GroupName" `
    -Headers $SaladHeaders `
    -ContentType "application/merge-patch+json" `
    -Body $PatchBody `
    -TimeoutSec 60 |
    Out-Null

$PatchBody = $null
$EnvironmentVariables["HF_TOKEN"] = $null
$HfToken = $null
$SaladApiKey = $null

Write-Host "HF_TOKEN applied to the stopped Phase 8 worker group." -ForegroundColor Green
