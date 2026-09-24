[CmdletBinding()]
param(
    [ValidateSet("qwen_image_21", "ltx25", "all")][string]$Service = "all",
    [string]$EnvFile = ".env"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
. (Join-Path $PSScriptRoot "_env_file.ps1")
Import-EnvFile -Path $EnvFile
if ([string]::IsNullOrWhiteSpace($env:SALAD_API_KEY)) {
    throw "SALAD_API_KEY is missing. Add it to .env before checking GPU availability."
}

$Manifest = Get-Content -LiteralPath (Join-Path $RepoRoot "deploy/salad/services.json") -Raw | ConvertFrom-Json
$Organization = [string]$Manifest.stack.organization
$BaseUrl = "https://api.salad.com/api/public/organizations/$Organization"
$Headers = @{
    "Salad-Api-Key" = $env:SALAD_API_KEY
    "Accept" = "application/json"
}
$Classes = Invoke-RestMethod -Method Get -Uri "$BaseUrl/gpu-classes" -Headers $Headers -TimeoutSec 30
$Services = if ($Service -eq "all") { @("ltx25", "qwen_image_21") } else { @($Service) }

foreach ($Name in $Services) {
    $Definition = $Manifest.services.$Name
    $ClassIds = @(
        foreach ($ClassName in @($Definition.resources.gpu_class_names)) {
            $Matched = @($Classes.items | Where-Object { [string]$_.name -eq [string]$ClassName })
            if ($Matched.Count -ne 1) {
                throw "Cannot resolve GPU class '$ClassName' for $Name in Salad."
            }
            [string]$Matched[0].id
        }
    )
    # The official availability endpoint considers CPU, RAM and storage jointly.
    $Body = @{
        gpu_classes = $ClassIds
        cpu = [int]$Definition.resources.cpu
        memory = [int]$Definition.resources.memory
        storage_amount = [long]$Definition.resources.storage_amount
    } | ConvertTo-Json -Depth 5
    $Availability = Invoke-RestMethod -Method Post -Uri "$BaseUrl/availability/sce-gpu-availability" -Headers $Headers -ContentType "application/json" -Body $Body -TimeoutSec 30
    $High = [int]$Availability.available_gpu_high
    [pscustomobject]@{
        service = $Name
        gpu_classes = (@($Definition.resources.gpu_class_names) -join ", ")
        priority = [string]$Definition.priority
        cpu = [int]$Definition.resources.cpu
        memory_mib = [int]$Definition.resources.memory
        storage_bytes = [long]$Definition.resources.storage_amount
        available_gpu_high = $High
        on_call_gpu = $Availability.on_call_gpu
    }
    if ($High -eq 0) {
        Write-Warning (
            "$Name has no currently available High-priority RTX 5090 matching its " +
            "CPU/RAM/storage requirements. This is a capacity snapshot, not a " +
            "reservation or proof that the service can never allocate."
        )
    }
}
