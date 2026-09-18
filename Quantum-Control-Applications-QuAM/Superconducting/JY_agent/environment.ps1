function Resolve-JyPython(
    [string]$AgentRoot,
    [string]$Override,
    $Saved,
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
    try {
        $env:PYTHONPATH = Join-Path $AgentRoot "src"
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
    }
    $Requirement = if ($RequireAgent) { "Qualibrate and JY_agent dependencies" } else { "Qualibrate" }
    throw (
        "No Python candidate could import $Requirement. Run repository-root " +
        "jy_agent.ps1 -Action Install with -Python."
    )
}

function Resolve-JyEnvironment(
    [string]$AgentRoot,
    [string]$Python,
    $Saved,
    [switch]$RequireAgent
) {
    $ResolvedPython = Resolve-JyPython `
        $AgentRoot $Python $Saved -RequireAgent:$RequireAgent
    $env:PYTHONPATH = Join-Path $AgentRoot "src"
    $env:JY_QUALIBRATE_PYTHON = $ResolvedPython
    return [ordered]@{
        python = $ResolvedPython
    }
}
