function Get-HttpStatusCode {
    param([Parameter(Mandatory)][object]$ErrorRecord)

    # Non-HTTP failures (DNS, proxy, timeouts, local errors) have no Response.
    # Under Set-StrictMode, reading a missing property hides the real exception.
    $ExceptionProperty = $ErrorRecord.PSObject.Properties["Exception"]
    if ($null -eq $ExceptionProperty -or $null -eq $ExceptionProperty.Value) {
        return $null
    }
    $ResponseProperty = $ExceptionProperty.Value.PSObject.Properties["Response"]
    if ($null -eq $ResponseProperty -or $null -eq $ResponseProperty.Value) {
        return $null
    }
    $StatusProperty = $ResponseProperty.Value.PSObject.Properties["StatusCode"]
    if ($null -eq $StatusProperty -or $null -eq $StatusProperty.Value) {
        return $null
    }
    try {
        return [int]$StatusProperty.Value
    }
    catch {
        return $null
    }
}
