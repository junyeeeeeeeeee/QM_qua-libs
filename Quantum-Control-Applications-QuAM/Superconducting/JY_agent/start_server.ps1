param(
    [Parameter(Mandatory = $false)]
    [string]$Python = $env:JY_QUALIBRATE_PYTHON,
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765,
    [string]$ApprovalHostAddress = "127.0.0.1",
    [int]$ApprovalPort = 8766
)

$ErrorActionPreference = "Stop"
$AgentRoot = $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}

$env:PYTHONPATH = Join-Path $AgentRoot "src"
$env:JY_QUALIBRATE_PYTHON = $Python
$env:JY_APPROVAL_HOST = $ApprovalHostAddress
$env:JY_APPROVAL_PORT = [string]$ApprovalPort

$Runtime = Join-Path $AgentRoot "runtime"
New-Item -ItemType Directory -Path $Runtime -Force | Out-Null
$ApprovalStdout = Join-Path $Runtime "approval.stdout.log"
$ApprovalStderr = Join-Path $Runtime "approval.stderr.log"
$ApprovalArgs = @(
    "-m", "jy_agent", "approval-serve",
    "--host", $ApprovalHostAddress,
    "--port", [string]$ApprovalPort
)
$ApprovalProcess = Start-Process `
    -FilePath $Python `
    -ArgumentList $ApprovalArgs `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $ApprovalStdout `
    -RedirectStandardError $ApprovalStderr

try {
    & $Python -m jy_agent serve --host $HostAddress --port $Port
    $ExitCode = $LASTEXITCODE
}
finally {
    if ($null -ne $ApprovalProcess -and -not $ApprovalProcess.HasExited) {
        Stop-Process -Id $ApprovalProcess.Id
        $ApprovalProcess.WaitForExit()
    }
}
exit $ExitCode
