param(
    [Parameter(Mandatory = $false)]
    [string]$Python = $env:JY_QUALIBRATE_PYTHON
)

$ErrorActionPreference = "Stop"
$AgentRoot = $PSScriptRoot
. (Join-Path $AgentRoot "environment.ps1")
$RepositoryRoot = (Resolve-Path -LiteralPath (
    Join-Path $AgentRoot "..\..\.."
)).Path

function Write-AtomicText([string]$Path, [string]$Value) {
    $Directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $Temporary = Join-Path $Directory ("." + [IO.Path]::GetFileName($Path) + "." + [guid]::NewGuid().ToString("N") + ".tmp")
    $Backup = Join-Path $Directory ("." + [IO.Path]::GetFileName($Path) + "." + [guid]::NewGuid().ToString("N") + ".bak")
    try {
        [IO.File]::WriteAllText(
            $Temporary,
            $Value,
            (New-Object Text.UTF8Encoding($false))
        )
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($Temporary, $Path, $Backup, $true)
        }
        else {
            [IO.File]::Move($Temporary, $Path)
        }
    }
    finally {
        Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $Backup -Force -ErrorAction SilentlyContinue
    }
}

$BootstrapPath = Join-Path $AgentRoot "runtime\server-bootstrap.json"
$Existing = $null
if (Test-Path -LiteralPath $BootstrapPath -PathType Leaf) {
    try {
        $Existing = Get-Content -Raw -LiteralPath $BootstrapPath | ConvertFrom-Json
    }
    catch {
        $Existing = $null
    }
}
$Environment = Resolve-JyEnvironment `
    $AgentRoot $Python $Existing
$Python = [string]$Environment.python

$RequiredProjectFiles = @(
    "AGENTS.md",
    "constraints.txt",
    "environment.ps1",
    "start_mcp_stdio.ps1",
    "diagnose_mcp.ps1"
)
$MissingProjectFiles = @(
    $RequiredProjectFiles | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $AgentRoot $_) -PathType Leaf)
    }
)
if ($MissingProjectFiles.Count -gt 0) {
    throw "JY project integration files are missing: $($MissingProjectFiles -join ', ')"
}

$RequiredRepositoryFiles = @(
    ".mcp.json",
    ".codex\config.toml.example",
    ".cursor\mcp.json",
    ".cursor\rules\jy-agent.mdc",
    ".agents\skills\jy-qubit-bringup\SKILL.md",
    "AGENTS.md",
    "CLAUDE.md",
    "jy_agent.ps1"
)
$MissingRepositoryFiles = @(
    $RequiredRepositoryFiles | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $RepositoryRoot $_) -PathType Leaf)
    }
)
if ($MissingRepositoryFiles.Count -gt 0) {
    throw (
        "Repository-root JY integration files are missing: " +
        ($MissingRepositoryFiles -join ', ')
    )
}

$Constraints = Join-Path $AgentRoot "constraints.txt"
& $Python -m pip install --constraint $Constraints --editable "${AgentRoot}[test]"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$env:PYTHONPATH = Join-Path $AgentRoot "src"
$env:JY_QUALIBRATE_PYTHON = $Python
& $Python -c "import qualibrate,mcp,yaml,jy_agent"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
& $Python -m jy_agent init
$InitExitCode = $LASTEXITCODE
if ($InitExitCode -eq 0) {
    $Runtime = Join-Path $AgentRoot "runtime"
    New-Item -ItemType Directory -Path $Runtime -Force | Out-Null
    $BootstrapPath = Join-Path $Runtime "server-bootstrap.json"
    $Existing = @{}
    if (Test-Path -LiteralPath $BootstrapPath -PathType Leaf) {
        try {
            $Existing = Get-Content -Raw -LiteralPath $BootstrapPath | ConvertFrom-Json
        }
        catch {
            $Existing = @{}
        }
    }
    $Bootstrap = [ordered]@{
        python = (Resolve-Path -LiteralPath $Python).Path
        remote_provider = if ([string]$Existing.remote_provider -in @("local", "public")) { [string]$Existing.remote_provider } else { "public" }
        public_base_url = [string]$Existing.public_base_url
        approval_access_token_path = [string]$Existing.approval_access_token_path
        approval_bootstrap_code_path = [string]$Existing.approval_bootstrap_code_path
        public_tunnel_pid = [int]$Existing.public_tunnel_pid
        mcp_pid = [int]$Existing.mcp_pid
        approval_pid = [int]$Existing.approval_pid
        instance_nonce = [string]$Existing.instance_nonce
        status = if ([string]$Existing.status) { [string]$Existing.status } else { "installed" }
        updated_at = [DateTimeOffset]::Now.ToString("o")
    }
    Write-AtomicText $BootstrapPath (($Bootstrap | ConvertTo-Json -Depth 4) + "`n")

    $CodexPath = Join-Path $RepositoryRoot ".codex\config.toml"
    $EscapedLauncher = (Join-Path $RepositoryRoot "jy_agent.ps1").Replace("\", "\\")
    $EscapedRoot = $RepositoryRoot.Replace("\", "\\")
    $CodexConfig = @"
#:schema https://developers.openai.com/codex/config-schema.json

# Generated for this checkout. Re-run Install after moving the repository.
[mcp_servers.jy_bringup]
command = "powershell.exe"
args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "$EscapedLauncher", "-Action", "Stdio"]
cwd = "$EscapedRoot"
required = false
startup_timeout_sec = 90
tool_timeout_sec = 120
default_tools_approval_mode = "auto"
"@
    Write-AtomicText $CodexPath ($CodexConfig + "`n")
}
if ($InitExitCode -eq 0) {
    Write-Host "JY_agent installation complete. Open the repository root as the agent workspace, accept the one-time MCP trust prompt, then say: 進入 JY 量測模式"
    Write-Host "JY_agent 安裝完成。請用 AI agent 開啟 repository 最上層、接受一次性的 MCP 信任提示，接著輸入：進入 JY 量測模式"
}
exit $InitExitCode
