[CmdletBinding()]
param(
    [ValidateRange(1, 30)]
    [int]$TimeoutMinutes = 5
)

$null = $TimeoutMinutes
$Manager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
Write-Warning (
    "start_phase8_autoscaled.ps1 is deprecated; forwarding to " +
    "manage_salad_worker.ps1 -Service ltx25 -Action Start."
)
& $Manager -Service ltx25 -Action Start
exit $LASTEXITCODE
