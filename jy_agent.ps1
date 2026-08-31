param(
    [ValidateSet("Stdio", "Ensure", "Stop", "Recover", "Install", "Doctor", "Resolve", "RecoverLock")]
    [string]$Action = "Stdio",
    [Parameter(Mandatory = $false)]
    [string]$Python = $env:JY_QUALIBRATE_PYTHON,
    [Parameter(Mandatory = $false)]
    [string]$QualibrateConfig = $env:QUALIBRATE_CONFIG_FILE,
    [Parameter(Mandatory = $false)]
    [string]$RunId,
    [switch]$InspectOnly
)

$ErrorActionPreference = "Stop"
$JyRelativePath = Join-Path "Quantum-Control-Applications-QuAM" (
    Join-Path "Superconducting" "JY_agent"
)
$JyAgentRoot = Join-Path $PSScriptRoot $JyRelativePath

if (-not (Test-Path -LiteralPath $JyAgentRoot -PathType Container)) {
    throw (
        "JY_agent was not found below this repository root: $JyAgentRoot. " +
        "Keep the repository's internal directory structure intact."
    )
}
$JyAgentRoot = (Resolve-Path -LiteralPath $JyAgentRoot).Path

if ($Action -eq "Resolve") {
    Write-Output $JyAgentRoot
    exit 0
}

$ScriptName = switch ($Action) {
    "Stdio" { "start_mcp_stdio.ps1" }
    "Ensure" { "ensure_server.ps1" }
    "Stop" { "stop_server.ps1" }
    "Recover" { "recover_closed_state.ps1" }
    "Install" { "setup.ps1" }
    "Doctor" { "diagnose_mcp.ps1" }
    "RecoverLock" { "recover_hardware_lock.ps1" }
}
$TargetScript = Join-Path $JyAgentRoot $ScriptName
if (-not (Test-Path -LiteralPath $TargetScript -PathType Leaf)) {
    throw "JY_agent entry script is missing: $TargetScript"
}

if ($Action -eq "RecoverLock") {
    if ([string]::IsNullOrWhiteSpace($RunId)) {
        throw "-RunId is required for RecoverLock."
    }
    & $TargetScript `
        -Python $Python `
        -QualibrateConfig $QualibrateConfig `
        -RunId $RunId `
        -InspectOnly:$InspectOnly
}
elseif ($Action -ne "Stop") {
    & $TargetScript -Python $Python -QualibrateConfig $QualibrateConfig
}
else {
    & $TargetScript
}
exit $LASTEXITCODE
