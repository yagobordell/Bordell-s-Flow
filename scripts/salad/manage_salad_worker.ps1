[CmdletBinding()]
param(
    [ValidateSet("Validate", "Prepare", "Start", "Status", "Stop")]
    [string]$Action = "Status",
    [string]$Service = "ltx25",
    [string]$Image = "",
    [string]$PinnedImage = "",
    [string]$EnvFile = ".env",
    [ValidateRange(10, 180)][int]$PrepareTimeoutMinutes = 120,
    [ValidateRange(0, 64)][int]$Replicas = 0,
    [switch]$SkipBuild,
    [switch]$AllowBootstrappingInstance,
    [switch]$Recreate,
    [switch]$AllowControllerOverride,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$ServicesPath = Join-Path $RepoRoot "deploy\salad\services.json"
if (-not (Test-Path -LiteralPath $ServicesPath -PathType Leaf)) {
    throw "Salad service manifest not found: $ServicesPath"
}

. (Join-Path $PSScriptRoot "_env_file.ps1")
. (Join-Path $PSScriptRoot "_http_status.ps1")
Import-EnvFile -Path $EnvFile

$Document = Get-Content -LiteralPath $ServicesPath -Raw | ConvertFrom-Json
if ([int]$Document.schema_version -ne 3) { throw "Salad manifest must use schema_version=3." }
if ([string]$Document.stack.job_transport -ne "postgres") {
    throw "Salad deployment must use Postgres as the canonical job transport."
}

$ServiceProperty = $Document.services.PSObject.Properties[$Service]
if ($null -eq $ServiceProperty) {
    throw "Unknown Salad service '$Service'."
}
$Definition = $ServiceProperty.Value
$Stack = $Document.stack

function Get-ManifestString {
    param([Parameter(Mandatory)][string]$Name, [object]$Primary, [object]$Fallback)
    $Value = ""
    if ($null -ne $Primary) {
        $Property = $Primary.PSObject.Properties[$Name]
        if ($null -ne $Property) { $Value = [string]$Property.Value }
    }
    if ([string]::IsNullOrWhiteSpace($Value) -and $null -ne $Fallback) {
        $Property = $Fallback.PSObject.Properties[$Name]
        if ($null -ne $Property) { $Value = [string]$Property.Value }
    }
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Salad manifest is missing '$Name' for '$Service'."
    }
    return $Value.Trim()
}

$Organization = Get-ManifestString -Name "organization" -Primary $Stack -Fallback $Definition
$Project = Get-ManifestString -Name "project" -Primary $Stack -Fallback $Definition
$GroupName = Get-ManifestString -Name "group_name" -Primary $Definition -Fallback $null
$Dockerfile = Join-Path $RepoRoot (Get-ManifestString -Name "dockerfile" -Primary $Definition -Fallback $null)
if ([string]::IsNullOrWhiteSpace($Image)) {
    $Image = Get-ManifestString -Name "image" -Primary $Definition -Fallback $null
}

$ContainerPort = [int]$Stack.container_port
$AutostartPolicy = [bool]$Stack.autostart_policy
$RestartPolicy = [string]$Stack.restart_policy
$ServiceAutostart = $Definition.PSObject.Properties["autostart_policy"]
if ($null -ne $ServiceAutostart) { $AutostartPolicy = [bool]$ServiceAutostart.Value }

$StartReplicas = [int]$Definition.capacity.start_replicas
$MaxReplicas = [int]$Definition.capacity.max_replicas
$DesiredReplicas = if ($Replicas -gt 0) { $Replicas } else { $StartReplicas }
$OrganizationApiBase = "https://api.salad.com/api/public/organizations/$Organization"
$GpuClassesBase = "$OrganizationApiBase/gpu-classes"
$ContainersBase = "$OrganizationApiBase/projects/$Project/containers"
$SecretNames = @("POSTGRES_DSN", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "HF_TOKEN", "SALAD_API_KEY")

function Resolve-CapacityLockPython {
    $VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        return $VenvPython
    }
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $Python) {
        throw (
            "Cannot verify Salad Capacity Controller ownership because Python is unavailable. " +
            "Create the project virtual environment or pass -AllowControllerOverride for an " +
            "intentional operator intervention."
        )
    }
    return [string]$Python.Source
}

function Enter-ManualCapacityMutationLock {
    if ($Action -notin @("Prepare", "Start", "Stop")) {
        return $null
    }
    if ($AllowControllerOverride) {
        Write-Warning (
            "Manual Salad capacity override requested without controller advisory-lock ownership."
        )
        return $null
    }

    $Python = Resolve-CapacityLockPython
    $LockScript = Join-Path $RepoRoot "scripts\salad\hold_capacity_controller_lock.py"
    if (-not (Test-Path -LiteralPath $LockScript -PathType Leaf)) {
        throw "Capacity Controller lock helper is missing: $LockScript"
    }

    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $Python
    $StartInfo.Arguments = '"' + $LockScript + '"'
    $StartInfo.WorkingDirectory = $RepoRoot
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardInput = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.CreateNoWindow = $true

    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    if (-not $Process.Start()) {
        throw "Could not start Capacity Controller lock helper."
    }

    $SignalTask = $Process.StandardOutput.ReadLineAsync()
    if (-not $SignalTask.Wait(10000)) {
        try { $Process.Kill() } catch {}
        $Process.WaitForExit()
        throw "Capacity Controller lock helper did not confirm ownership within 10 seconds."
    }
    $Signal = $SignalTask.Result
    if ($Signal -ne "LOCK_ACQUIRED") {
        $ErrorText = $Process.StandardError.ReadToEnd().Trim()
        $Process.WaitForExit()
        $Detail = if ([string]::IsNullOrWhiteSpace($ErrorText)) {
            "lock helper returned '$Signal'"
        }
        else {
            $ErrorText
        }
        throw (
            "Manual Salad action '$Action' is blocked because controller ownership " +
            "could not be acquired: $Detail. Stop the singleton Capacity Controller first, " +
            "or pass -AllowControllerOverride for an intentional operator intervention."
        )
    }

    return $Process
}

function Assert-ManualCapacityMutationAuthority {
    if ($Action -notin @("Prepare", "Start", "Stop") -or $AllowControllerOverride) {
        return
    }
    $Process = $script:CapacityMutationLock
    if ($null -eq $Process) {
        throw "Manual Salad action '$Action' has no Capacity Controller ownership lock."
    }
    try {
        if ($Process.HasExited) {
            $ErrorText = $Process.StandardError.ReadToEnd().Trim()
            $Detail = if ([string]::IsNullOrWhiteSpace($ErrorText)) {
                "capacity lock helper exited unexpectedly"
            }
            else {
                $ErrorText
            }
            throw "Manual Salad action '$Action' lost Capacity Controller ownership: $Detail"
        }
        $Process.StandardInput.WriteLine("check")
        $Process.StandardInput.Flush()
        $SignalTask = $Process.StandardOutput.ReadLineAsync()
        if (-not $SignalTask.Wait(5000)) {
            throw "capacity lock helper did not confirm ownership within 5 seconds"
        }
        $Signal = $SignalTask.Result
        if ($Signal -ne "LOCK_OK") {
            $ErrorText = if ($Process.HasExited) {
                $Process.StandardError.ReadToEnd().Trim()
            }
            else {
                ""
            }
            $Detail = if (-not [string]::IsNullOrWhiteSpace($ErrorText)) {
                $ErrorText
            }
            else {
                "capacity lock helper returned '$Signal'"
            }
            throw "Manual Salad action '$Action' lost Capacity Controller ownership: $Detail"
        }
    }
    catch {
        if ($_.Exception.Message -match "lost Capacity Controller ownership") {
            throw
        }
        throw (
            "Manual Salad action '$Action' lost Capacity Controller ownership: " +
            $_.Exception.Message
        )
    }
}

function Wait-ManualCapacityMutationInterval {
    param([Parameter(Mandatory)][double]$Seconds)
    if ($Seconds -le 0) {
        Assert-ManualCapacityMutationAuthority
        return
    }
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        Assert-ManualCapacityMutationAuthority
        $RemainingMilliseconds = [Math]::Max(
            0,
            [int]([DateTime]::UtcNow.Subtract($Deadline).TotalMilliseconds * -1)
        )
        if ($RemainingMilliseconds -le 0) {
            break
        }
        Start-Sleep -Milliseconds ([Math]::Min(1000, $RemainingMilliseconds))
    } while ([DateTime]::UtcNow -lt $Deadline)
    Assert-ManualCapacityMutationAuthority
}

function Exit-ManualCapacityMutationLock {
    param([object]$Process)
    if ($null -eq $Process) {
        return
    }
    try {
        if (-not $Process.HasExited) {
            $Process.StandardInput.WriteLine("release")
            $Process.StandardInput.Close()
            if (-not $Process.WaitForExit(5000)) {
                $Process.Kill()
                $Process.WaitForExit()
            }
        }
    }
    finally {
        $Process.Dispose()
    }
}

function Get-RequiredEnvironmentNames {
    $Names = @()
    $Shared = $Stack.PSObject.Properties["shared_required_environment"]
    if ($null -ne $Shared) { $Names += @($Shared.Value | ForEach-Object { [string]$_ }) }
    $Required = $Definition.PSObject.Properties["required_environment"]
    if ($null -ne $Required) { $Names += @($Required.Value | ForEach-Object { [string]$_ }) }
    return @($Names | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
}

function Assert-ServiceDefinition {
    if (-not (Test-Path -LiteralPath $Dockerfile -PathType Leaf)) { throw "Missing Dockerfile: $Dockerfile" }
    if ($Image -notmatch '^[^\s]+:[^\s/:]+$') { throw "Image must include an explicit tag: $Image" }
    if (-not [string]::IsNullOrWhiteSpace($PinnedImage) -and $PinnedImage -notmatch '^[^\s]+@sha256:[0-9a-fA-F]{64}$') {
        throw "-PinnedImage must be an immutable sha256 reference."
    }
    if ($ContainerPort -ne 8080) { throw "Shared worker container port must remain 8080." }
    if ($RestartPolicy -notin @("always", "on_failure", "never")) { throw "Unsupported restart policy '$RestartPolicy'." }
    if ($StartReplicas -lt 1) { throw "capacity.start_replicas must be at least 1." }
    if ($MaxReplicas -lt $StartReplicas) { throw "capacity.max_replicas must be >= start_replicas." }
    if ($DesiredReplicas -gt $MaxReplicas) { throw "replicas=$DesiredReplicas exceeds max_replicas=$MaxReplicas." }
    if (@($Definition.resources.gpu_class_names).Count -eq 0) { throw "At least one GPU class name is required." }
}

function Get-Setting {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Prompt, [switch]$Secret)
    $Value = [Environment]::GetEnvironmentVariable($Name, [EnvironmentVariableTarget]::Process)
    if (-not [string]::IsNullOrWhiteSpace($Value)) { return $Value.Trim() }
    if ($NonInteractive) { throw "$Name is missing and -NonInteractive was requested." }
    if ($Secret) {
        $SecureValue = Read-Host $Prompt -AsSecureString
        $Value = [PSCredential]::new("salad-worker", $SecureValue).GetNetworkCredential().Password
    }
    else {
        $Value = Read-Host $Prompt
    }
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Name is empty." }
    $Value = $Value.Trim()
    [Environment]::SetEnvironmentVariable($Name, $Value, [EnvironmentVariableTarget]::Process)
    return $Value
}

function Get-Headers {
    $ApiKey = Get-Setting -Name "SALAD_API_KEY" -Prompt "Salad API key" -Secret
    return @{
        "Salad-Api-Key" = $ApiKey
        "Accept" = "application/json"
        "User-Agent" = "ai-video-factory-worker-manager/3.0"
    }
}

function Test-TransientSaladFailure {
    param([Parameter(Mandatory)][object]$ErrorRecord)
    $StatusCode = Get-HttpStatusCode -ErrorRecord $ErrorRecord
    if ($StatusCode -in @(408, 429, 500, 502, 503, 504)) { return $true }
    return [string]$ErrorRecord.Exception.Message -match "(?i)timed out|timeout|disconnect|reset|server unavailable|gateway timeout"
}

function Invoke-SaladRequest {
    param(
        [Parameter(Mandatory)][hashtable]$Headers,
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Operation,
        [string]$Method = "Get",
        [ValidateRange(1, 120)][int]$TimeoutSec = 30,
        [ValidateRange(1, 10)][int]$MaxAttempts = 6,
        [string]$ContentType = "",
        [object]$Body = $null
    )
    $Mutating = $Method -in @("Post", "Patch", "Delete")
    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt += 1) {
        try {
            if ($Mutating) {
                Assert-ManualCapacityMutationAuthority
            }
            $Request = @{ Method = $Method; Uri = $Uri; Headers = $Headers; TimeoutSec = $TimeoutSec }
            if (-not [string]::IsNullOrWhiteSpace($ContentType)) { $Request["ContentType"] = $ContentType }
            if ($null -ne $Body) { $Request["Body"] = $Body }
            $Response = Invoke-RestMethod @Request
            if ($Mutating) {
                Assert-ManualCapacityMutationAuthority
            }
            return $Response
        }
        catch {
            if ($Mutating) {
                Assert-ManualCapacityMutationAuthority
            }
            if (-not (Test-TransientSaladFailure -ErrorRecord $_) -or $Attempt -ge $MaxAttempts) { throw }
            $Delay = [Math]::Min(15, 2 * $Attempt)
            Write-Warning "$Service Salad '$Operation' transient failure ($Attempt/$MaxAttempts); retrying in $Delay seconds."
            Wait-ManualCapacityMutationInterval -Seconds $Delay
        }
    }
}

function Resolve-GpuClassIds {
    param([Parameter(Mandatory)][hashtable]$Headers)
    $Response = Invoke-SaladRequest -Headers $Headers -Uri $GpuClassesBase -Operation "list GPU classes"
    $Items = if ($Response.PSObject.Properties.Name -contains "items") { @($Response.items) } else { @($Response) }
    $Resolved = @()
    foreach ($ConfiguredName in @($Definition.resources.gpu_class_names)) {
        $MatchesByName = @($Items | Where-Object { [string]$_.name -eq [string]$ConfiguredName })
        if ($MatchesByName.Count -ne 1) { throw "GPU class '$ConfiguredName' did not resolve exactly once." }
        $Resolved += [string]$MatchesByName[0].id
    }
    return $Resolved
}

function Get-GroupStatus {
    param([Parameter(Mandatory)][object]$Group)
    $State = $Group.PSObject.Properties["current_state"]
    if ($null -eq $State -or $null -eq $State.Value) { return "unknown" }
    $Status = $State.Value.PSObject.Properties["status"]
    if ($null -eq $Status -or $null -eq $Status.Value) { return "unknown" }
    return [string]$Status.Value
}

function Try-Get-Group {
    param([Parameter(Mandatory)][hashtable]$Headers)
    try {
        return Invoke-SaladRequest -Headers $Headers -Uri "$ContainersBase/$GroupName" -Operation "read container group"
    }
    catch {
        if ((Get-HttpStatusCode -ErrorRecord $_) -eq 404) { return $null }
        throw
    }
}

function Get-Group {
    param([Parameter(Mandatory)][hashtable]$Headers)
    $Group = Try-Get-Group -Headers $Headers
    if ($null -eq $Group) { throw "Container group '$GroupName' does not exist. Run Prepare first." }
    return $Group
}

function Wait-ForGroupSettled {
    param(
        [Parameter(Mandatory)][hashtable]$Headers,
        [ValidateRange(1, 180)][int]$TimeoutMinutes,
        [switch]$AllowInitialNotFound
    )
    $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $VisibilityDeadline = (Get-Date).AddMinutes(5)
    do {
        Wait-ManualCapacityMutationInterval -Seconds 10
        $Group = if ($AllowInitialNotFound) {
            Try-Get-Group -Headers $Headers
        }
        else {
            Get-Group -Headers $Headers
        }
        if ($null -eq $Group) {
            if ((Get-Date) -ge $VisibilityDeadline) {
                throw "Salad did not expose newly created '$GroupName' within 5 minutes."
            }
            Write-Host ("{0} service={1} waiting for created group visibility" -f (Get-Date -Format "HH:mm:ss"), $Service)
            continue
        }
        Write-Host ("{0} service={1} status={2} replicas={3} pending={4}" -f (Get-Date -Format "HH:mm:ss"), $Service, (Get-GroupStatus -Group $Group), [int]$Group.replicas, [bool]$Group.pending_change)
        if (-not [bool]$Group.pending_change) { return $Group }
    } while ((Get-Date) -lt $Deadline)
    throw "Salad did not settle '$Service' before timeout."
}

function Wait-ForRunningCapacity {
    param(
        [Parameter(Mandatory)][hashtable]$Headers,
        [Parameter(Mandatory)][int]$ExpectedReplicas,
        [switch]$AllowBootstrappingInstance
    )
    $Deadline = (Get-Date).AddMinutes(30)
    $LastObservation = ""
    $LastReportAt = [DateTime]::MinValue
    $PullInstanceId = ""
    $LastPullProgress = -1.0
    $LastPullProgressAt = [DateTime]::UtcNow
    $RequestedReallocations = @{}
    $ReallocationCount = 0
    $MaxPullReallocations = 2
    $PullStallSeconds = 480

    do {
        Wait-ManualCapacityMutationInterval -Seconds 10
        $Group = Get-Group -Headers $Headers
        $Status = Get-GroupStatus -Group $Group
        $ReplicasMatch = ([int]$Group.replicas -eq $ExpectedReplicas)
        $Settled = (-not [bool]$Group.pending_change)

        if (-not $AllowBootstrappingInstance -and $Status -eq "running" -and
            $Settled -and $ReplicasMatch) {
            return $Group
        }

        $InstanceSummary = "not-requested"
        if ($AllowBootstrappingInstance) {
            $Response = Invoke-SaladRequest -Headers $Headers -Uri "$ContainersBase/$GroupName/instances" -Operation "list LTX startup instances"
            $Items = if ($null -ne $Response.PSObject.Properties["instances"]) {
                @($Response.instances)
            } elseif ($null -ne $Response.PSObject.Properties["items"]) {
                @($Response.items)
            } else {
                throw "Salad instances response is missing the instances collection."
            }
            $Current = @(
                $Items | Where-Object {
                    $null -ne $_ -and [int]$_.version -eq [int]$Group.version -and
                    [string]$_.state -in @("allocating", "downloading", "creating", "running")
                }
            )
            if ($Current.Count -eq 1) {
                $Instance = $Current[0]
                $Id = [string]$Instance.id
                $InstanceState = [string]$Instance.state
                $Started = ($Instance.started -eq $true)
                $Ready = ($Instance.ready -eq $true)
                $Pull = $Instance.PSObject.Properties["pulling_progress"]
                $Progress = if ($null -ne $Pull -and $null -ne $Pull.Value) {
                    [double]$Pull.Value
                } else { -1.0 }
                $InstanceSummary = "id=$Id state=$InstanceState started=$Started ready=$Ready pulling_progress=$Progress"
                if ($ReplicasMatch -and $Settled -and $Started -and
                    $InstanceState -eq "running" -and
                    $Status -in @("running", "deploying", "pending")) {
                    Write-Host (
                        "service=$Service LTX instance started id=$Id; " +
                        "worker readiness will be checked before Postgres submission."
                    ) -ForegroundColor Green
                    return $Group
                }

                # Keep nodes that have started: their network preflight and
                # byte-progress watchdog assess actual Hugging Face throughput.
                # Only consider host-side reallocation while the Docker image
                # itself is stalled, before the container can run its watchdog.
                if ($Id -ne $PullInstanceId) {
                    $PullInstanceId = $Id
                    $LastPullProgress = $Progress
                    $LastPullProgressAt = [DateTime]::UtcNow
                } elseif ($InstanceState -ne "downloading" -or $Started -or $Progress -lt 0) {
                    $LastPullProgress = $Progress
                    $LastPullProgressAt = [DateTime]::UtcNow
                } elseif ($Progress -gt $LastPullProgress + 0.01) {
                    $LastPullProgress = $Progress
                    $LastPullProgressAt = [DateTime]::UtcNow
                } elseif ($ReplicasMatch -and $Settled -and
                    ([DateTime]::UtcNow - $LastPullProgressAt).TotalSeconds -ge $PullStallSeconds -and
                    $ReallocationCount -lt $MaxPullReallocations -and
                    -not $RequestedReallocations.ContainsKey($Id)) {
                    $ParsedId = [guid]::Empty
                    if (-not [guid]::TryParse($Id, [ref]$ParsedId)) {
                        throw "Salad returned an invalid LTX instance UUID; refusing reallocation."
                    }
                    # Verify the group and the same image pull again immediately
                    # before the mutation, under the Capacity Controller lock.
                    $ConfirmGroup = Get-Group -Headers $Headers
                    $Confirm = Invoke-SaladRequest -Headers $Headers -Uri "$ContainersBase/$GroupName/instances" -Operation "confirm stalled LTX image pull"
                    $ConfirmItems = if ($null -ne $Confirm.PSObject.Properties["instances"]) {
                        @($Confirm.instances)
                    } elseif ($null -ne $Confirm.PSObject.Properties["items"]) {
                        @($Confirm.items)
                    } else {
                        throw "Salad confirmation response is missing instances."
                    }
                    $Matching = @($ConfirmItems | Where-Object { [string]$_.id -eq $Id })
                    if ([int]$ConfirmGroup.version -eq [int]$Group.version -and
                        [int]$ConfirmGroup.replicas -eq $ExpectedReplicas -and
                        -not [bool]$ConfirmGroup.pending_change -and
                        (Get-GroupStatus -Group $ConfirmGroup) -in @("running", "deploying", "pending") -and
                        $Matching.Count -eq 1 -and
                        [int]$Matching[0].version -eq [int]$Group.version -and
                        [string]$Matching[0].state -eq "downloading" -and
                        $Matching[0].started -ne $true -and
                        $null -ne $Matching[0].pulling_progress -and
                        [double]$Matching[0].pulling_progress -le $LastPullProgress + 0.01) {
                        Write-Warning (
                            "LTX Docker image pull stalled on $Id for $PullStallSeconds seconds " +
                            "(progress=$Progress). Salad reallocation " +
                            "$($ReallocationCount + 1)/$MaxPullReallocations."
                        )
                        Invoke-SaladRequest -Headers $Headers -Method "Post" -Uri "$ContainersBase/$GroupName/instances/$Id/reallocate" -Operation "reallocate stalled LTX image-pull instance" -TimeoutSec 60 | Out-Null
                        $RequestedReallocations[$Id] = $true
                        $ReallocationCount += 1
                    } else {
                        $LastPullProgressAt = [DateTime]::UtcNow
                    }
                }
            } else {
                $InstanceSummary = "current_version_instances=$($Current.Count)"
                $PullInstanceId = ""
            }
        }
        $Observation = "status=$Status replicas=$($Group.replicas) pending=$($Group.pending_change) $InstanceSummary"
        if ($Observation -ne $LastObservation -or
            ([DateTime]::UtcNow - $LastReportAt).TotalSeconds -ge 60) {
            Write-Host ("{0} service={1} start_wait {2}" -f (Get-Date -Format "HH:mm:ss"), $Service, $Observation)
            $LastObservation = $Observation
            $LastReportAt = [DateTime]::UtcNow
        }
    } while ((Get-Date) -lt $Deadline)
    throw (
        "'$GroupName' did not start a current-version worker or reach " +
        "running/replicas=$ExpectedReplicas before the 30-minute capacity deadline; " +
        "last_observation=$LastObservation image_pull_reallocations=$ReallocationCount."
    )
}

function Wait-ForStoppedGroup {
    param([Parameter(Mandatory)][hashtable]$Headers)
    $Deadline = (Get-Date).AddMinutes(3)
    $StableReads = 0
    do {
        Wait-ManualCapacityMutationInterval -Seconds 5
        $Group = Get-Group -Headers $Headers
        if ((Get-GroupStatus -Group $Group) -eq "stopped" -and -not [bool]$Group.pending_change) {
            $StableReads += 1
            if ($StableReads -ge 2) { return $Group }
        }
        else { $StableReads = 0 }
    } while ((Get-Date) -lt $Deadline)
    throw "'$GroupName' did not converge to stable stopped/pending_change=false."
}

function Resolve-PinnedImage {
    param([Parameter(Mandatory)][string]$MutableImage)
    $Inspect = & docker buildx imagetools inspect $MutableImage 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Published image cannot be inspected: $MutableImage" }
    $Digest = $null
    foreach ($Line in $Inspect) {
        if ([string]$Line -match '^\s*Digest:\s+(sha256:[0-9a-f]{64})\s*$') { $Digest = $Matches[1]; break }
    }
    if ($null -eq $Digest) { throw "Could not resolve image digest for $MutableImage." }
    if ($MutableImage -notmatch '^(?<repo>.+):[^/:]+$') { throw "Image must include an explicit tag." }
    return "$([string]$Matches['repo'])@$Digest"
}

function New-Probe {
    param([Parameter(Mandatory)][object]$Probe)
    return @{
        http = @{ headers = @(); path = [string]$Probe.path; port = $ContainerPort; scheme = "http" }
        initial_delay_seconds = 0
        period_seconds = [int]$Probe.period_seconds
        failure_threshold = [int]$Probe.failure_threshold
        success_threshold = 1
        timeout_seconds = [int]$Probe.timeout_seconds
    }
}

function Get-WorkerEnvironment {
    $Environment = @{}
    foreach ($Property in @($Stack.shared_environment.PSObject.Properties) + @($Definition.environment.PSObject.Properties)) {
        $Environment[$Property.Name] = [string]$Property.Value
    }
    foreach ($Name in Get-RequiredEnvironmentNames) {
        $Environment[[string]$Name] = Get-Setting -Name ([string]$Name) -Prompt ([string]$Name) -Secret:($SecretNames -contains [string]$Name)
    }
    return $Environment
}

function New-ContainerConfiguration {
    param([Parameter(Mandatory)][string]$ResolvedImage, [Parameter(Mandatory)][hashtable]$WorkerEnvironment, [Parameter(Mandatory)][string[]]$GpuClassIds)
    return @{
        image = $ResolvedImage
        resources = @{
            cpu = [int]$Definition.resources.cpu
            memory = [int]$Definition.resources.memory
            gpu_classes = $GpuClassIds
            shm_size = [int]$Definition.resources.shm_size
            storage_amount = [Int64]$Definition.resources.storage_amount
        }
        environment_variables = $WorkerEnvironment
        image_caching = $true
        priority = [string]$Definition.priority
    }
}

function Test-LegacyQueueAttachment {
    param([Parameter(Mandatory)][object]$Group)
    foreach ($Name in @("queue_connection", "queue_autoscaler")) {
        $Property = $Group.PSObject.Properties[$Name]
        if ($null -ne $Property -and $null -ne $Property.Value) { return $true }
    }
    return $false
}

function Remove-StoppedContainerGroup {
    param([Parameter(Mandatory)][hashtable]$Headers)
    $Group = Get-Group -Headers $Headers
    if ((Get-GroupStatus -Group $Group) -ne "stopped" -or [bool]$Group.pending_change) {
        throw "'$GroupName' must be stably stopped before recreation."
    }
    Invoke-SaladRequest -Headers $Headers -Method "Delete" -Uri "$ContainersBase/$GroupName" -Operation "delete legacy container group" -TimeoutSec 60 | Out-Null
    $Deadline = (Get-Date).AddMinutes(5)
    do {
        Wait-ManualCapacityMutationInterval -Seconds 5
        if ($null -eq (Try-Get-Group -Headers $Headers)) { return }
    } while ((Get-Date) -lt $Deadline)
    throw "'$GroupName' was not deleted within 5 minutes."
}

function New-ContainerGroup {
    param([Parameter(Mandatory)][hashtable]$Headers, [Parameter(Mandatory)][string]$ResolvedImage, [Parameter(Mandatory)][hashtable]$WorkerEnvironment, [Parameter(Mandatory)][string[]]$GpuClassIds)
    $CreateBody = @{
        name = $GroupName
        display_name = [string]$Definition.display_name
        autostart_policy = $AutostartPolicy
        replicas = $StartReplicas
        restart_policy = $RestartPolicy
        scheduled_scaling_enabled = $false
        container = New-ContainerConfiguration -ResolvedImage $ResolvedImage -WorkerEnvironment $WorkerEnvironment -GpuClassIds $GpuClassIds
        startup_probe = New-Probe -Probe $Definition.probes.startup
        readiness_probe = New-Probe -Probe $Definition.probes.readiness
        liveness_probe = New-Probe -Probe $Stack.shared_liveness_probe
    } | ConvertTo-Json -Depth 20

    $Deadline = (Get-Date).AddMinutes(5)
    do {
        try {
            Invoke-SaladRequest -Headers $Headers -Method "Post" -Uri $ContainersBase -Operation "create container group" -ContentType "application/json" -Body $CreateBody -TimeoutSec 60 | Out-Null
            return
        }
        catch {
            $Details = ""
            if ($null -ne $_.ErrorDetails) {
                $Details = [string]$_.ErrorDetails.Message
            }
            if ((Get-HttpStatusCode -ErrorRecord $_) -ne 400 -or $Details -notmatch 'name_conflict' -or (Get-Date) -ge $Deadline) { throw }
            Wait-ManualCapacityMutationInterval -Seconds 10
        }
    } while ((Get-Date) -lt $Deadline)
}

function Update-ContainerGroup {
    param([Parameter(Mandatory)][hashtable]$Headers, [Parameter(Mandatory)][string]$ResolvedImage, [Parameter(Mandatory)][hashtable]$WorkerEnvironment, [Parameter(Mandatory)][string[]]$GpuClassIds)
    $PatchBody = @{
        container = New-ContainerConfiguration -ResolvedImage $ResolvedImage -WorkerEnvironment $WorkerEnvironment -GpuClassIds $GpuClassIds
        startup_probe = New-Probe -Probe $Definition.probes.startup
        readiness_probe = New-Probe -Probe $Definition.probes.readiness
        liveness_probe = New-Probe -Probe $Stack.shared_liveness_probe
    } | ConvertTo-Json -Depth 20
    Invoke-SaladRequest -Headers $Headers -Method "Patch" -Uri "$ContainersBase/$GroupName" -Operation "update container group" -ContentType "application/merge-patch+json" -Body $PatchBody -TimeoutSec 60 | Out-Null
}

function Assert-PreparedGroup {
    param([Parameter(Mandatory)][object]$Group, [Parameter(Mandatory)][string]$ResolvedImage)
    if ([string]$Group.container.image -ne $ResolvedImage) { throw "Salad did not activate expected image." }
    $Priority = $Group.PSObject.Properties["priority"]
    if ($null -eq $Priority -or [string]$Priority.Value -ne [string]$Definition.priority) {
        throw "Salad did not report the expected container group priority."
    }
    if ((Get-GroupStatus -Group $Group) -ne "stopped" -or [bool]$Group.pending_change) {
        throw "Prepared group must remain stably stopped."
    }
    if (Test-LegacyQueueAttachment -Group $Group) { throw "Legacy Salad queue attachment remains after Prepare." }
}

function Show-Status {
    param([Parameter(Mandatory)][hashtable]$Headers)
    $Group = Try-Get-Group -Headers $Headers
    if ($null -eq $Group) {
        Write-Host "service=$Service group=$GroupName status=missing" -ForegroundColor Yellow
        return
    }
    Write-Host ("service={0} group={1} status={2} replicas={3} pending={4} transport=postgres legacy_queue={5} capacity={6}-{7} image={8}" -f $Service, $GroupName, (Get-GroupStatus -Group $Group), [int]$Group.replicas, [bool]$Group.pending_change, (Test-LegacyQueueAttachment -Group $Group), $StartReplicas, $MaxReplicas, [string]$Group.container.image)
}

Assert-ServiceDefinition
Set-Location $RepoRoot
if ($Recreate -and $Action -ne "Prepare") { throw "-Recreate is only valid with Prepare." }
if ($AllowBootstrappingInstance -and ($Action -ne "Start" -or $Service -ne "ltx25" -or
    $DesiredReplicas -ne 1)) {
    throw "-AllowBootstrappingInstance is only valid for LTX Start with one replica."
}

$script:CapacityMutationLock = $null
try {
    $script:CapacityMutationLock = Enter-ManualCapacityMutationLock
    Assert-ManualCapacityMutationAuthority

    if ($Action -eq "Validate") {
        Write-Host ("VALID service={0} group={1} transport=postgres capacity={2}-{3}" -f $Service, $GroupName, $StartReplicas, $MaxReplicas) -ForegroundColor Green
        exit 0
    }

    $Headers = Get-Headers

    switch ($Action) {
    "Status" {
        Show-Status -Headers $Headers
        exit 0
    }
    "Stop" {
        $Group = Try-Get-Group -Headers $Headers
        if ($null -eq $Group) { Write-Host "$Service group does not exist." -ForegroundColor Green; exit 0 }
        if ((Get-GroupStatus -Group $Group) -ne "stopped") {
            Invoke-SaladRequest -Headers $Headers -Method "Post" -Uri "$ContainersBase/$GroupName/stop" -Operation "stop container group" -TimeoutSec 60 | Out-Null
            $Group = Wait-ForGroupSettled -Headers $Headers -TimeoutMinutes 30
        }
        $Stopped = Wait-ForStoppedGroup -Headers $Headers
        Write-Host ("{0} stopped; configured replicas={1}." -f $Service, [int]$Stopped.replicas) -ForegroundColor Green
        exit 0
    }
    "Start" {
        $Group = Get-Group -Headers $Headers
        if (Test-LegacyQueueAttachment -Group $Group) {
            throw "'$Service' still has Salad Job Queue config. Run Prepare once to migrate it."
        }
        if ([int]$Group.replicas -ne $DesiredReplicas) {
            $Body = @{ replicas = $DesiredReplicas } | ConvertTo-Json
            Invoke-SaladRequest -Headers $Headers -Method "Patch" -Uri "$ContainersBase/$GroupName" -Operation "set explicit replica capacity" -ContentType "application/merge-patch+json" -Body $Body -TimeoutSec 60 | Out-Null
            $Group = Wait-ForGroupSettled -Headers $Headers -TimeoutMinutes 30
        }
        if ((Get-GroupStatus -Group $Group) -ne "running") {
            Invoke-SaladRequest -Headers $Headers -Method "Post" -Uri "$ContainersBase/$GroupName/start" -Operation "start container group" -TimeoutSec 60 | Out-Null
        }
        Wait-ForRunningCapacity -Headers $Headers -ExpectedReplicas $DesiredReplicas -AllowBootstrappingInstance:$AllowBootstrappingInstance | Out-Null
        if ($AllowBootstrappingInstance) {
            Write-Host "$Service LTX bootstrap instance started; awaiting /ready before submitting Postgres demand." -ForegroundColor Green
        } else {
            Write-Host "$Service running with explicit replicas=$DesiredReplicas; Postgres owns demand." -ForegroundColor Green
        }
        exit 0
    }
    "Prepare" {
        $Existing = Try-Get-Group -Headers $Headers
        if ($null -ne $Existing) {
            if ((Get-GroupStatus -Group $Existing) -ne "stopped" -or [bool]$Existing.pending_change) {
                throw "'$GroupName' must be stably stopped before Prepare."
            }
            if ($Recreate -or (Test-LegacyQueueAttachment -Group $Existing)) {
                if (Test-LegacyQueueAttachment -Group $Existing) {
                    Write-Host "Recreating '$GroupName' to remove Salad Job Queue/autoscaler state." -ForegroundColor Cyan
                }
                Remove-StoppedContainerGroup -Headers $Headers
                $Existing = $null
            }
        }

        if (-not [string]::IsNullOrWhiteSpace($PinnedImage)) {
            $ResolvedImage = $PinnedImage.Trim()
        }
        elseif ($SkipBuild -and $null -ne $Existing -and [string]$Existing.container.image -match '@sha256:[0-9a-fA-F]{64}$') {
            $ResolvedImage = [string]$Existing.container.image
        }
        else {
            if (-not $SkipBuild) {
                & docker version *> $null
                if ($LASTEXITCODE -ne 0) { throw "Docker is not running." }
                & docker buildx build --platform linux/amd64 --file $Dockerfile --tag $Image --push .
                if ($LASTEXITCODE -ne 0) { throw "Docker build or push failed." }
                Assert-ManualCapacityMutationAuthority
            }
            $ResolvedImage = Resolve-PinnedImage -MutableImage $Image
        }

        $WorkerEnvironment = Get-WorkerEnvironment
        $GpuClassIds = @(Resolve-GpuClassIds -Headers $Headers)
        if ($null -eq $Existing) {
            New-ContainerGroup -Headers $Headers -ResolvedImage $ResolvedImage -WorkerEnvironment $WorkerEnvironment -GpuClassIds $GpuClassIds
        }
        else {
            Update-ContainerGroup -Headers $Headers -ResolvedImage $ResolvedImage -WorkerEnvironment $WorkerEnvironment -GpuClassIds $GpuClassIds
        }

        $Prepared = Wait-ForGroupSettled -Headers $Headers -TimeoutMinutes $PrepareTimeoutMinutes -AllowInitialNotFound
        if ((Get-GroupStatus -Group $Prepared) -ne "stopped") { throw "Prepared group must remain stopped." }
        $Prepared = Wait-ForStoppedGroup -Headers $Headers
        Assert-PreparedGroup -Group $Prepared -ResolvedImage $ResolvedImage
        Write-Host ("{0} prepared without Salad queue/autoscaler; stopped with configured replicas={1}." -f $Service, [int]$Prepared.replicas) -ForegroundColor Green
        exit 0
    }
}
}
finally {
    Exit-ManualCapacityMutationLock -Process $script:CapacityMutationLock
}
