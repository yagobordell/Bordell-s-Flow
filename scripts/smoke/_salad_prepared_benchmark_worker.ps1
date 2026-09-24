function Resolve-SaladBenchmarkPinnedImage {
    [CmdletBinding()]
    param([Parameter(Mandatory)][object]$Definition)

    $Image = [string]$Definition.image
    $Inspect = & docker buildx imagetools inspect $Image 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot inspect published benchmark image '$Image'. Prepare the service first."
    }
    $Digest = $null
    foreach ($Line in $Inspect) {
        if ([string]$Line -match '^\s*Digest:\s+(sha256:[0-9a-f]{64})\s*$') {
            $Digest = $Matches[1]
            break
        }
    }
    $TagMatch = [regex]::Match($Image, '^(?<repo>.+):[^/:]+$')
    if ($null -eq $Digest -or -not $TagMatch.Success) {
        throw "Could not resolve an immutable digest for benchmark image '$Image'."
    }
    return "$($TagMatch.Groups['repo'].Value)@$Digest"
}

function Assert-SaladPreparedBenchmarkWorker {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][object]$Group,
        [Parameter(Mandatory)][object]$Definition
    )

    if (
        [string]$Group.current_state.status -ne "stopped" -or
        [int]$Group.replicas -ne 0 -or
        [bool]$Group.pending_change
    ) {
        throw "Benchmark requires a prepared stopped group with replicas=0/pending=False."
    }
    $RemoteAutoscaler = $Group.PSObject.Properties["queue_autoscaler"]
    if (
        $null -ne $RemoteAutoscaler -and
        $null -ne $RemoteAutoscaler.Value -and
        [int]$RemoteAutoscaler.Value.min_replicas -ne 0
    ) {
        throw (
            "Benchmark requires Salad queue_autoscaler.min_replicas=0; " +
            "restore the manifest before starting the GPU worker."
        )
    }
    if ([string]$Group.priority -ne [string]$Definition.priority) {
        throw "Salad group priority does not match the benchmark manifest."
    }
    if ([string]$Group.queue_connection.queue_name -ne [string]$Definition.queue_name) {
        throw "Salad group queue does not match the benchmark manifest."
    }

    # A mutable Docker tag is not proof that Salad is running the benchmark image.
    $PinnedImage = Resolve-SaladBenchmarkPinnedImage -Definition $Definition
    if ([string]$Group.container.image -ne $PinnedImage) {
        throw (
            "Salad does not use the published benchmark image. " +
            "Expected=$PinnedImage actual=$($Group.container.image). Run Prepare first."
        )
    }

    Write-Host "Reusing prepared benchmark image: $PinnedImage" -ForegroundColor Green
}
