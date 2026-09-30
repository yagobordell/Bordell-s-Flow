function Import-EnvFile {
    param([Parameter(Mandatory)][string]$Path)

    $Resolved = $Path
    if (-not [IO.Path]::IsPathRooted($Resolved)) {
        $Resolved = Join-Path $RepoRoot $Resolved
    }
    $ConfigFiles = @(
        (Join-Path $RepoRoot "config\app.env"),
        (Join-Path $RepoRoot "config\salad.env"),
        $Resolved
    )

    foreach ($ConfigFile in $ConfigFiles) {
        if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
            continue
        }
        foreach ($RawLine in Get-Content -LiteralPath $ConfigFile) {
            $Line = $RawLine.Trim()
            if ([string]::IsNullOrWhiteSpace($Line) -or $Line.StartsWith("#")) {
                continue
            }
            if ($Line -notmatch '^(?:export\s+)?(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$') {
                continue
            }
            $Name = $Matches["name"]
            $Value = $Matches["value"].Trim()
            if ($Value.Length -ge 2) {
                $First = $Value.Substring(0, 1)
                $Last = $Value.Substring($Value.Length - 1, 1)
                if (($First -eq '"' -and $Last -eq '"') -or ($First -eq "'" -and $Last -eq "'")) {
                    $Value = $Value.Substring(1, $Value.Length - 2)
                }
            }
            $Existing = [Environment]::GetEnvironmentVariable(
                $Name,
                [EnvironmentVariableTarget]::Process
            )
            if ([string]::IsNullOrWhiteSpace($Existing)) {
                [Environment]::SetEnvironmentVariable(
                    $Name,
                    $Value,
                    [EnvironmentVariableTarget]::Process
                )
            }
        }
    }
}
