function Resolve-JyQualibrateConfig(
    [string]$AgentRoot,
    [string]$Override,
    $Saved
) {
    $Candidates = @(
        $Override,
        $env:QUALIBRATE_CONFIG_FILE,
        $(if ($null -ne $Saved) { [string]$Saved.qualibrate_config } else { "" }),
        $(Join-Path $env:USERPROFILE ".qualibrate\config.toml")
    )
    foreach ($CandidateValue in $Candidates) {
        if ([string]::IsNullOrWhiteSpace([string]$CandidateValue)) {
            continue
        }
        $Candidate = [string]$CandidateValue
        if (Test-Path -LiteralPath $Candidate -PathType Container) {
            $Candidate = Join-Path $Candidate "config.toml"
        }
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    throw (
        "Qualibrate config.toml was not found. Pass -QualibrateConfig once or " +
        "set QUALIBRATE_CONFIG_FILE; the resolved path is persisted for later launches."
    )
}

function Resolve-JyPython(
    [string]$AgentRoot,
    [string]$Override,
    $Saved,
    [string]$QualibrateConfig,
    [switch]$RequireAgent
) {
    $Candidates = @(
        $Override,
        $env:JY_QUALIBRATE_PYTHON,
        $(if ($null -ne $Saved) { [string]$Saved.python } else { "" }),
        $(Join-Path $env:USERPROFILE "anaconda3\envs\qualibrate_env\python.exe"),
        $(Join-Path $env:USERPROFILE "miniconda3\envs\qualibrate_env\python.exe")
    )
    $PathPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $PathPython) {
        $Candidates += $PathPython.Source
    }
    $PreviousPythonPath = $env:PYTHONPATH
    $PreviousConfig = $env:QUALIBRATE_CONFIG_FILE
    try {
        $env:PYTHONPATH = Join-Path $AgentRoot "src"
        $env:QUALIBRATE_CONFIG_FILE = $QualibrateConfig
        foreach ($CandidateValue in ($Candidates | Select-Object -Unique)) {
            if ([string]::IsNullOrWhiteSpace([string]$CandidateValue) -or
                -not (Test-Path -LiteralPath ([string]$CandidateValue) -PathType Leaf)) {
                continue
            }
            $Candidate = (Resolve-Path -LiteralPath ([string]$CandidateValue)).Path
            $Probe = if ($RequireAgent) {
                "import importlib.util,sys; names=('qualibrate','mcp','yaml','jy_agent'); sys.exit(0 if all(importlib.util.find_spec(n) for n in names) else 1)"
            }
            else {
                "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('qualibrate') else 1)"
            }
            & $Candidate -c $Probe *> $null
            if ($LASTEXITCODE -eq 0) {
                return $Candidate
            }
        }
    }
    finally {
        $env:PYTHONPATH = $PreviousPythonPath
        $env:QUALIBRATE_CONFIG_FILE = $PreviousConfig
    }
    $Requirement = if ($RequireAgent) { "Qualibrate and JY_agent dependencies" } else { "Qualibrate" }
    throw (
        "No Python candidate could import $Requirement. Run repository-root " +
        "jy_agent.ps1 -Action Install with -Python and -QualibrateConfig."
    )
}

function Resolve-JyEnvironment(
    [string]$AgentRoot,
    [string]$Python,
    [string]$QualibrateConfig,
    $Saved,
    [switch]$RequireAgent
) {
    $ResolvedConfig = Resolve-JyQualibrateConfig $AgentRoot $QualibrateConfig $Saved
    $ResolvedPython = Resolve-JyPython `
        $AgentRoot $Python $Saved $ResolvedConfig -RequireAgent:$RequireAgent
    $env:PYTHONPATH = Join-Path $AgentRoot "src"
    $env:JY_QUALIBRATE_PYTHON = $ResolvedPython
    $env:QUALIBRATE_CONFIG_FILE = $ResolvedConfig
    return [ordered]@{
        python = $ResolvedPython
        qualibrate_config = $ResolvedConfig
    }
}
