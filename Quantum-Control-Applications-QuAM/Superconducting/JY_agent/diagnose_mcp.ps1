param(
    [Parameter(Mandatory = $false)]
    [string]$Python = $env:JY_QUALIBRATE_PYTHON,
    [Parameter(Mandatory = $false)]
    [string]$QualibrateConfig = $env:QUALIBRATE_CONFIG_FILE,
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"
$AgentRoot = $PSScriptRoot
. (Join-Path $AgentRoot "environment.ps1")
$RepositoryRoot = (Resolve-Path -LiteralPath (
    Join-Path $AgentRoot "..\..\.."
)).Path
$BootstrapPath = Join-Path $AgentRoot "runtime\server-bootstrap.json"
$Saved = $null
if (Test-Path -LiteralPath $BootstrapPath -PathType Leaf) {
    try {
        $Saved = Get-Content -Raw -LiteralPath $BootstrapPath | ConvertFrom-Json
    }
    catch {
        $Saved = $null
    }
}
$Environment = Resolve-JyEnvironment `
    $AgentRoot $Python $QualibrateConfig $Saved -RequireAgent
$ResolvedPython = [string]$Environment.python

& $ResolvedPython -m jy_agent doctor `
    --repository-root $RepositoryRoot `
    --timeout-seconds $TimeoutSeconds
exit $LASTEXITCODE
