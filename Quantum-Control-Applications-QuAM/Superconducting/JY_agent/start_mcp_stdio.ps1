param(
    [Parameter(Mandatory = $false)]
    [string]$Python = $env:JY_QUALIBRATE_PYTHON,
    [Parameter(Mandatory = $false)]
    [string]$QualibrateConfig = $env:QUALIBRATE_CONFIG_FILE
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$AgentRoot = $PSScriptRoot
. (Join-Path $AgentRoot "environment.ps1")
$BootstrapPath = Join-Path $AgentRoot "runtime\server-bootstrap.json"
$Bootstrap = $null
if (Test-Path -LiteralPath $BootstrapPath -PathType Leaf) {
    try {
        $Bootstrap = Get-Content -Raw -LiteralPath $BootstrapPath | ConvertFrom-Json
    }
    catch {
        $Bootstrap = $null
    }
}

# STDIO initialization must never depend on the Dashboard, Cloudflare, ports, or
# network access. Those services are idempotently ensured after an entry phrase.
# Keeping this launcher protocol-only prevents a tunnel failure from closing the
# MCP connection before the initialize response.
$Environment = Resolve-JyEnvironment `
    $AgentRoot $Python $QualibrateConfig $Bootstrap -RequireAgent
$ResolvedPython = [string]$Environment.python
$LaunchLog = Join-Path $AgentRoot "runtime\mcp-stdio-launch.log"
New-Item -ItemType Directory -Path (Split-Path -Parent $LaunchLog) -Force |
    Out-Null
Add-Content -LiteralPath $LaunchLog -Encoding UTF8 -Value (
    "{0} pid={1} python={2} config={3} cwd={4}" -f
    [DateTimeOffset]::Now.ToString("o"), $PID, $ResolvedPython,
    [string]$Environment.qualibrate_config, (Get-Location).Path
)

& $ResolvedPython -m jy_agent stdio
exit $LASTEXITCODE
