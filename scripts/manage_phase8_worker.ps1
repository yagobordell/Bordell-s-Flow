[CmdletBinding()]
param(
    [ValidateSet("Prepare", "Start", "Status", "Stop")]
    [string]$Action = "Status",

    [string]$Image = "",

    [ValidateRange(10, 180)]
    [int]$PrepareTimeoutMinutes = 120,

    [switch]$SkipBuild
)

$Manager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
$Arguments = @(
    "-Service", "ltx25",
    "-Action", $Action,
    "-PrepareTimeoutMinutes", $PrepareTimeoutMinutes
)
if (-not [string]::IsNullOrWhiteSpace($Image)) {
    $Arguments += @("-Image", $Image)
}
if ($SkipBuild) {
    $Arguments += "-SkipBuild"
}

Write-Warning (
    "manage_phase8_worker.ps1 is deprecated; forwarding to " +
    "manage_salad_worker.ps1 -Service ltx25."
)
& $Manager @Arguments
exit $LASTEXITCODE
