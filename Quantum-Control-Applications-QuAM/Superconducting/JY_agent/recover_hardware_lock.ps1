param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{32}$')]
    [string]$RunId,
    [Parameter(Mandatory = $false)]
    [string]$Python = $env:JY_QUALIBRATE_PYTHON,
    [switch]$InspectOnly
)

$ErrorActionPreference = "Stop"
$AgentRoot = $PSScriptRoot
. (Join-Path $AgentRoot "environment.ps1")
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
    $AgentRoot $Python $Saved -RequireAgent
$ResolvedPython = [string]$Environment.python
$Arguments = @("-m", "jy_agent", "recover-lock", $RunId)
if ($InspectOnly) {
    $Arguments += "--inspect-only"
}
& $ResolvedPython @Arguments
exit $LASTEXITCODE
