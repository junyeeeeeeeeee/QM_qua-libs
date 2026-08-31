param(
    [Parameter(Mandatory = $true)]
    [string]$ProposalId,
    [Parameter(Mandatory = $false)]
    [string]$Python = $env:JY_QUALIBRATE_PYTHON
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
& $Python -m jy_agent approve $ProposalId
exit $LASTEXITCODE
