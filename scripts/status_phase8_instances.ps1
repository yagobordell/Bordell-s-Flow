[CmdletBinding()]
param(
    [ValidateRange(1, 120)]
    [int]$Samples = 1,

    [ValidateRange(1, 300)]
    [int]$IntervalSeconds = 30
)

$Manager = Join-Path $PSScriptRoot "manage_salad_worker.ps1"
Write-Warning (
    "status_phase8_instances.ps1 is deprecated; forwarding to " +
    "manage_salad_worker.ps1 -Service ltx25 -Action Status."
)

for ($Sample = 1; $Sample -le $Samples; $Sample++) {
    & $Manager -Service ltx25 -Action Status
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    if ($Sample -lt $Samples) {
        Start-Sleep -Seconds $IntervalSeconds
    }
}
