function Import-EnvFile {
    param([Parameter(Mandatory)][string]$Path)

    $Resolved = $Path
    if (-not [IO.Path]::IsPathRooted($Resolved)) {
        $Resolved = Join-Path $RepoRoot $Resolved
    }
    # Mirror the Python smoke tools, which also read the checkout's .env.
    $ConfigFiles = @(
        (Join-Path $RepoRoot "config\app.env"),
        (Join-Path $RepoRoot "config\salad.env"),
        (Join-Path $RepoRoot ".env")
    )

    # A caller operating in a worktree may explicitly select the main
    # checkout's config/salad.env. Read its sibling app.env and root .env as
    # well, so the capacity-lock helper sees the same PostgreSQL DSN.
    $Resolved = [IO.Path]::GetFullPath($Resolved)
    $ConfigDir = Split-Path -Parent $Resolved
    if ((Split-Path -Leaf $Resolved) -eq "salad.env" -and
        (Split-Path -Leaf $ConfigDir) -eq "config") {
        $ExternalRoot = Split-Path -Parent $ConfigDir
        if (-not [string]::Equals(
            [IO.Path]::GetFullPath($ExternalRoot).TrimEnd('\', '/'),
            [IO.Path]::GetFullPath($RepoRoot).TrimEnd('\', '/'),
            [StringComparison]::OrdinalIgnoreCase
        )) {
            $ConfigFiles += @(
                (Join-Path $ConfigDir "app.env"),
                (Join-Path $ExternalRoot ".env")
            )
        }
    }
    $ConfigFiles += $Resolved
    $ConfigFiles = @($ConfigFiles | Select-Object -Unique)

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
