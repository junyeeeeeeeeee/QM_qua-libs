param(
    [Parameter(Mandatory = $false)]
    [string]$Python = $env:JY_QUALIBRATE_PYTHON,
    [Parameter(Mandatory = $false)]
    [string]$QualibrateConfig = $env:QUALIBRATE_CONFIG_FILE,
    [ValidateSet("", "local", "public")]
    [string]$RemoteProvider = $env:JY_REMOTE_ACCESS_PROVIDER,
    [int]$McpPort = 8765,
    [int]$ApprovalPort = 8766,
    [int]$StartupTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$AgentRoot = $PSScriptRoot
. (Join-Path $AgentRoot "environment.ps1")
$Runtime = Join-Path $AgentRoot "runtime"
$BootstrapPath = Join-Path $Runtime "server-bootstrap.json"
New-Item -ItemType Directory -Path $Runtime -Force | Out-Null

function Write-AtomicText([string]$Path, [string]$Value, [System.Text.Encoding]$Encoding) {
    $Directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $Temporary = Join-Path $Directory ("." + [IO.Path]::GetFileName($Path) + "." + [guid]::NewGuid().ToString("N") + ".tmp")
    $Backup = Join-Path $Directory ("." + [IO.Path]::GetFileName($Path) + "." + [guid]::NewGuid().ToString("N") + ".bak")
    try {
        [IO.File]::WriteAllText($Temporary, $Value, $Encoding)
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

function Write-AtomicJson([string]$Path, $Value) {
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    Write-AtomicText $Path (($Value | ConvertTo-Json -Depth 8) + "`n") $Utf8NoBom
}

function Set-PrivateFileAcl([string]$Path) {
    if (-not $IsWindows -and $null -ne (Get-Variable IsWindows -ErrorAction SilentlyContinue)) {
        return
    }
    try {
        $Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls.exe $Path /inheritance:r /grant:r "${Identity}:(F)" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "icacls exited with code $LASTEXITCODE"
        }
    }
    catch {
        throw "Could not restrict the runtime secret ACL at ${Path}: $($_.Exception.Message)"
    }
}

function Read-BootstrapConfig {
    if (-not (Test-Path -LiteralPath $BootstrapPath -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -Raw -LiteralPath $BootstrapPath | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Resolve-CloudflaredExecutable {
    $Command = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
    if ($null -ne $Command) {
        return $Command.Source
    }
    $Tools = Join-Path $Runtime "tools"
    $Installed = Join-Path $Tools "cloudflared.exe"
    if (Test-Path -LiteralPath $Installed -PathType Leaf) {
        return $Installed
    }
    New-Item -ItemType Directory -Path $Tools -Force | Out-Null
    $Temporary = Join-Path $Tools ("cloudflared-" + [guid]::NewGuid().ToString("N") + ".tmp")
    try {
        Write-Host "[public] cloudflared is missing; downloading the official Windows amd64 binary once..."
        Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
            -OutFile $Temporary `
            -TimeoutSec 120
        Move-Item -LiteralPath $Temporary -Destination $Installed
        $Version = & $Installed version
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($Version -join ""))) {
            throw "The downloaded cloudflared executable did not pass its version check."
        }
        return $Installed
    }
    catch {
        Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
        throw (
            "cloudflared is required for the public JY dashboard and could not " +
            "be installed: $($_.Exception.Message)"
        )
    }
}

function Read-OrCreate-PublicAccessToken {
    $TokenPath = Join-Path $Runtime "public-dashboard-access.token"
    if (Test-Path -LiteralPath $TokenPath -PathType Leaf) {
        $Existing = (Get-Content -Raw -LiteralPath $TokenPath).Trim()
        if ($Existing.Length -ge 64) {
            Set-PrivateFileAcl $TokenPath
            return [ordered]@{ path = $TokenPath; value = $Existing }
        }
        throw "The existing public dashboard access token is invalid."
    }
    $Bytes = New-Object byte[] 32
    $Generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $Generator.GetBytes($Bytes)
    }
    finally {
        $Generator.Dispose()
    }
    $Token = ([BitConverter]::ToString($Bytes) -replace "-", "").ToLowerInvariant()
    Write-AtomicText $TokenPath $Token ([Text.Encoding]::ASCII)
    Set-PrivateFileAcl $TokenPath
    return [ordered]@{ path = $TokenPath; value = $Token }
}

function New-PublicBootstrapCode {
    $CodePath = Join-Path $Runtime "public-dashboard-bootstrap.json"
    $Bytes = New-Object byte[] 32
    $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $Generator.GetBytes($Bytes)
    }
    finally {
        $Generator.Dispose()
    }
    $Code = ([BitConverter]::ToString($Bytes) -replace "-", "").ToLowerInvariant()
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $HashBytes = $Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Code))
    }
    finally {
        $Hasher.Dispose()
    }
    $CodeHash = ([BitConverter]::ToString($HashBytes) -replace "-", "").ToLowerInvariant()
    $Record = [ordered]@{
        code = $Code
        code_sha256 = $CodeHash
        # Keep fractional seconds within Python 3.10's six-digit ISO parser
        # limit. Python also accepts older seven-digit records defensively.
        created_at = [DateTimeOffset]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.ffffffK")
        expires_at = [DateTimeOffset]::UtcNow.AddMinutes(10).ToString("yyyy-MM-ddTHH:mm:ss.ffffffK")
        used = $false
    }
    Write-AtomicJson $CodePath $Record
    Set-PrivateFileAcl $CodePath
    return [ordered]@{ path = $CodePath; value = $Code }
}

function Resolve-PublicTunnel([string]$Executable, [string]$Target, $SavedConfig) {
    $Stdout = Join-Path $Runtime "public-tunnel.stdout.log"
    $Stderr = Join-Path $Runtime "public-tunnel.stderr.log"
    $SavedPid = if ($null -ne $SavedConfig) { [int]$SavedConfig.public_tunnel_pid } else { 0 }
    $SavedUrl = if ($null -ne $SavedConfig) { [string]$SavedConfig.public_base_url } else { "" }
    if ($SavedPid -gt 0 -and $SavedUrl -match '^https://[a-zA-Z0-9.-]+\.trycloudflare\.com$') {
        $Process = Get-Process -Id $SavedPid -ErrorAction SilentlyContinue
        $ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $SavedPid" `
            -ErrorAction SilentlyContinue
        $CommandLine = [string]$ProcessInfo.CommandLine
        if ($null -ne $Process -and $Process.ProcessName -eq "cloudflared" -and
            $null -ne $ProcessInfo -and $CommandLine -like "*tunnel*" -and
            $CommandLine -like "*$Target*") {
            return [ordered]@{ pid = $SavedPid; url = $SavedUrl; reused = $true }
        }
    }
    if ($SavedPid -gt 0) {
        $Stale = Get-Process -Id $SavedPid -ErrorAction SilentlyContinue
        if ($null -ne $Stale -and $Stale.ProcessName -eq "cloudflared") {
            Stop-Process -Id $SavedPid
        }
    }
    Set-Content -LiteralPath $Stdout -Value "" -Encoding UTF8
    Set-Content -LiteralPath $Stderr -Value "" -Encoding UTF8
    $Tunnel = Start-Process `
        -FilePath $Executable `
        -ArgumentList @("tunnel", "--no-autoupdate", "--url", $Target) `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $Stdout `
        -RedirectStandardError $Stderr
    $Deadline = [DateTime]::UtcNow.AddSeconds(30)
    $PublicUrl = ""
    do {
        $Process = Get-Process -Id $Tunnel.Id -ErrorAction SilentlyContinue
        if ($null -eq $Process) {
            break
        }
        $LogText = ((Get-Content -Raw -LiteralPath $Stdout -ErrorAction SilentlyContinue) + "`n" +
            (Get-Content -Raw -LiteralPath $Stderr -ErrorAction SilentlyContinue))
        $Match = [regex]::Match($LogText, 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com')
        if ($Match.Success) {
            $PublicUrl = $Match.Value.TrimEnd("/")
            break
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $Deadline)
    if ([string]::IsNullOrWhiteSpace($PublicUrl)) {
        Stop-Process -Id $Tunnel.Id -ErrorAction SilentlyContinue
        $Tail = Get-Content -LiteralPath $Stderr -Tail 30 -ErrorAction SilentlyContinue
        throw "Cloudflare quick tunnel did not provide a public URL. $($Tail -join ' ')"
    }
    return [ordered]@{ pid = $Tunnel.Id; url = $PublicUrl; reused = $false }
}

function Format-UrlHost([string]$HostAddress) {
    if ($HostAddress.Contains(":")) {
        return "[$HostAddress]"
    }
    return $HostAddress
}

function Read-Health([string]$Uri, [string]$HealthToken = "") {
    try {
        $Headers = @{}
        if (-not [string]::IsNullOrWhiteSpace($HealthToken)) {
            $Headers["X-JY-Health-Token"] = $HealthToken
        }
        return Invoke-RestMethod -UseBasicParsing -Uri $Uri -Headers $Headers -TimeoutSec 3
    }
    catch {
        return $null
    }
}

function Test-HealthIdentity($Health, [string]$Service, [string]$Nonce) {
    return (
        $null -ne $Health -and
        [string]$Health.service -eq $Service -and
        -not [string]::IsNullOrWhiteSpace($Nonce) -and
        [string]$Health.instance_nonce -eq $Nonce
    )
}

function Stop-SavedPublicTunnel($SavedConfig, [int]$Port) {
    if ($null -eq $SavedConfig -or [int]$SavedConfig.public_tunnel_pid -le 0) {
        return
    }
    $TunnelPid = [int]$SavedConfig.public_tunnel_pid
    $Process = Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue
    if ($null -eq $Process) {
        return
    }
    $Info = Get-CimInstance Win32_Process -Filter "ProcessId = $TunnelPid" `
        -ErrorAction SilentlyContinue
    $CommandLine = [string]$Info.CommandLine
    $ExpectedTarget = "http://127.0.0.1:$Port"
    if ($Process.ProcessName -ne "cloudflared" -or $null -eq $Info -or
        $CommandLine -notlike "*tunnel*" -or
        $CommandLine -notlike "*$ExpectedTarget*") {
        throw "Refusing to stop an unverified saved public tunnel process."
    }
    Stop-Process -Id $TunnelPid
    $Process.WaitForExit(5000) | Out-Null
    if ($null -ne (Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue)) {
        throw "The saved public tunnel did not stop within five seconds."
    }
}

function Test-ListeningPort([string]$HostAddress, [int]$Port) {
    $Client = New-Object System.Net.Sockets.TcpClient
    try {
        $Result = $Client.BeginConnect($HostAddress, $Port, $null, $null)
        return $Result.AsyncWaitHandle.WaitOne(500) -and $Client.Connected
    }
    catch {
        return $false
    }
    finally {
        $Client.Close()
    }
}

function Get-LiveJyWorkers([string]$ExpectedAgentRoot) {
    $ExpectedRequests = Join-Path $ExpectedAgentRoot "runtime\requests"
    try {
        return @(
            Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                $Line = [string]$_.CommandLine
                -not [string]::IsNullOrWhiteSpace($Line) -and
                $Line -like "*-m jy_agent.worker*" -and
                $Line -like "*$ExpectedRequests*"
            }
        )
    }
    catch {
        throw "Unable to verify JY workers before choosing normal or recovery-only startup."
    }
}

function Stop-VerifiedJyProcess(
    $Health,
    [string]$ExpectedService,
    [string]$ExpectedNonce,
    [string]$ExpectedCommandFragment,
    [string]$ExpectedPython
) {
    if ($null -eq $Health) {
        return
    }
    if ([string]$Health.service -ne $ExpectedService -or
        [string]$Health.instance_nonce -ne $ExpectedNonce) {
        throw "Refusing to stop an unidentified listener on a JY port."
    }
    if ($ExpectedService -eq "jy-mcp" -and [bool]$Health.active_run) {
        throw "JY has an active hardware run; remote-service restart is deferred."
    }
    $ProcessId = [int]$Health.pid
    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -ne $Process) {
        $Info = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" `
            -ErrorAction SilentlyContinue
        $CommandLine = [string]$Info.CommandLine
        $Executable = [string]$Info.ExecutablePath
        if ($null -eq $Info -or
            $CommandLine -notlike "*$ExpectedCommandFragment*" -or
            -not [IO.Path]::GetFullPath($Executable).Equals(
                [IO.Path]::GetFullPath($ExpectedPython),
                [StringComparison]::OrdinalIgnoreCase
            )) {
            throw "Refusing to stop a PID that does not match the managed JY command."
        }
        Stop-Process -Id $ProcessId
        $Process.WaitForExit(5000) | Out-Null
        if ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            throw "Managed JY process did not stop within five seconds."
        }
    }
}

function Stop-VerifiedLegacyJyListener(
    [int]$Port,
    [string]$ExpectedCommandFragment
) {
    $Connections = Get-NetTCPConnection -State Listen -LocalPort $Port `
        -ErrorAction SilentlyContinue
    if ($null -eq $Connections) {
        return
    }
    $OwnerIds = @($Connections | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($OwnerId in $OwnerIds) {
        $ProcessInfo = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $OwnerId" -ErrorAction SilentlyContinue
        $CommandLine = [string]$ProcessInfo.CommandLine
        if ($null -eq $ProcessInfo -or
            $CommandLine -notlike "*$ExpectedCommandFragment*") {
            throw "Port $Port is occupied by a listener that cannot identify as managed JY."
        }
        if ($ExpectedCommandFragment -eq "-m jy_agent serve") {
            $HardwareLock = Join-Path $AgentRoot "runtime\hardware.lock"
            if ((Test-Path -LiteralPath $HardwareLock -PathType Leaf) -and
                -not $RecoveryOnly) {
                throw (
                    "A JY hardware lock exists; legacy-service migration is " +
                    "deferred until an operator inspects the run."
                )
            }
        }
        Stop-Process -Id ([int]$OwnerId)
        $Process = Get-Process -Id ([int]$OwnerId) -ErrorAction SilentlyContinue
        if ($null -ne $Process) {
            $Process.WaitForExit(5000) | Out-Null
        }
    }
}

$MutexHasher = [Security.Cryptography.SHA256]::Create()
try {
    $MutexHash = $MutexHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($AgentRoot.ToLowerInvariant()))
}
finally {
    $MutexHasher.Dispose()
}
$MutexSuffix = (([BitConverter]::ToString($MutexHash) -replace "-", "").Substring(0, 24))
$LifecycleMutex = New-Object Threading.Mutex($false, "Local\JYAgentLifecycle-$MutexSuffix")
$LifecycleAcquired = $false
$StartedPids = @()
$StartedTunnelPid = 0
try {
    $LifecycleAcquired = $LifecycleMutex.WaitOne([TimeSpan]::FromSeconds(60))
    if (-not $LifecycleAcquired) {
        throw "Timed out waiting for the JY lifecycle lock."
    }

$Saved = Read-BootstrapConfig
$Environment = Resolve-JyEnvironment `
    $AgentRoot $Python $QualibrateConfig $Saved -RequireAgent
$ResolvedPython = [string]$Environment.python
$ResolvedQualibrateConfig = [string]$Environment.qualibrate_config
$HardwareLock = Join-Path $Runtime "hardware.lock"
$RecoveryOnly = $false
if (Test-Path -LiteralPath $HardwareLock -PathType Leaf) {
    $RecoveryOnly = @(Get-LiveJyWorkers $AgentRoot).Count -eq 0
}
if ($RecoveryOnly) {
    # A retained lock with no live worker is a recovery quarantine.  Never
    # recreate a public tunnel until an operator has recovered the lock locally.
    $RemoteProvider = "local"
}
if ([string]::IsNullOrWhiteSpace($RemoteProvider)) {
    # Local is a temporary recovery quarantine, not a persistent user
    # preference.  Every normal entry defaults back to the token-protected
    # public Dashboard unless the caller explicitly requests local mode.
    $RemoteProvider = "public"
}
$RemoteProvider = $RemoteProvider.Trim().ToLowerInvariant()
$ApprovalTransport = $RemoteProvider
$ApprovalBindHost = "127.0.0.1"
$PublicBaseUrl = ""
$ApprovalAccessToken = ""
$ApprovalAccessTokenPath = ""
$ApprovalBootstrapCode = ""
$ApprovalBootstrapCodePath = ""
$PublicTunnelPid = 0

if ($RemoteProvider -eq "public") {
    $Cloudflared = Resolve-CloudflaredExecutable
    $TokenInfo = Read-OrCreate-PublicAccessToken
    $ApprovalAccessToken = [string]$TokenInfo.value
    $ApprovalAccessTokenPath = [string]$TokenInfo.path
    $BootstrapCodeInfo = New-PublicBootstrapCode
    $ApprovalBootstrapCode = [string]$BootstrapCodeInfo.value
    $ApprovalBootstrapCodePath = [string]$BootstrapCodeInfo.path
    $ApprovalTarget = "http://127.0.0.1:$ApprovalPort"
    $TunnelInfo = Resolve-PublicTunnel $Cloudflared $ApprovalTarget $Saved
    $PublicTunnelPid = [int]$TunnelInfo.pid
    $PublicBaseUrl = [string]$TunnelInfo.url
    if (-not [bool]$TunnelInfo.reused) {
        $StartedTunnelPid = $PublicTunnelPid
    }
}
elseif ($RemoteProvider -eq "local") {
    Stop-SavedPublicTunnel $Saved $ApprovalPort
    if ($RecoveryOnly) {
        Remove-Item -LiteralPath (Join-Path $Runtime "public-dashboard-access.token") `
            -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $Runtime "public-dashboard-bootstrap.json") `
            -Force -ErrorAction SilentlyContinue
    }
}
else {
    throw "Unsupported remote provider: $RemoteProvider"
}

$McpHealthUri = "http://127.0.0.1:$McpPort/healthz"
$ApprovalHealthUri = "http://$(Format-UrlHost $ApprovalBindHost):$ApprovalPort/healthz"
$SavedNonce = if ($null -ne $Saved) { [string]$Saved.instance_nonce } else { "" }
$PreviousPython = if ($null -ne $Saved) { [string]$Saved.python } else { $ResolvedPython }
$HealthToken = $ApprovalAccessToken
if ([string]::IsNullOrWhiteSpace($HealthToken) -and $null -ne $Saved) {
    $SavedTokenPath = [string]$Saved.approval_access_token_path
    if (-not [string]::IsNullOrWhiteSpace($SavedTokenPath) -and
        (Test-Path -LiteralPath $SavedTokenPath -PathType Leaf)) {
        $HealthToken = (Get-Content -Raw -LiteralPath $SavedTokenPath).Trim()
    }
}
$McpHealth = Read-Health $McpHealthUri
$ApprovalHealth = Read-Health $ApprovalHealthUri $HealthToken
if (-not (Test-HealthIdentity $McpHealth "jy-mcp" $SavedNonce)) {
    $McpHealth = $null
}
if (-not (Test-HealthIdentity $ApprovalHealth "jy-approval" $SavedNonce)) {
    $ApprovalHealth = $null
}
$PreviousApprovalHost = if ($null -ne $Saved -and -not [string]::IsNullOrWhiteSpace([string]$Saved.approval_bind_host)) {
    [string]$Saved.approval_bind_host
}
else {
    "127.0.0.1"
}
if ($PreviousApprovalHost -ne $ApprovalBindHost) {
    $PreviousApprovalHealthUri = "http://$(Format-UrlHost $PreviousApprovalHost):$ApprovalPort/healthz"
    $PreviousApprovalHealth = Read-Health $PreviousApprovalHealthUri $HealthToken
    if (Test-HealthIdentity $PreviousApprovalHealth "jy-approval" $SavedNonce) {
        Stop-VerifiedJyProcess $PreviousApprovalHealth "jy-approval" $SavedNonce `
            "-m jy_agent approval-serve" $PreviousPython
    }
}
$ConfigurationChanged = (
    $null -eq $Saved -or
    [string]$Saved.remote_provider -ne $RemoteProvider -or
    [bool]$Saved.recovery_only -ne $RecoveryOnly -or
    [string]$Saved.approval_bind_host -ne $ApprovalBindHost -or
    [string]$Saved.public_base_url -ne $PublicBaseUrl -or
    [string]$Saved.python -ne $ResolvedPython -or
    [string]$Saved.qualibrate_config -ne $ResolvedQualibrateConfig
)
$RemoteApprovalExpected = $RemoteProvider -eq "public"
$WrongMode = (
    ($null -ne $McpHealth -and [bool]$McpHealth.remote_approval_enabled -ne $RemoteApprovalExpected) -or
    ($null -ne $ApprovalHealth -and [bool]$ApprovalHealth.remote_approval_enabled -ne $RemoteApprovalExpected)
)
if ($ConfigurationChanged -or $WrongMode) {
    Stop-VerifiedJyProcess $McpHealth "jy-mcp" $SavedNonce `
        "-m jy_agent serve" $PreviousPython
    Stop-VerifiedJyProcess $ApprovalHealth "jy-approval" $SavedNonce `
        "-m jy_agent approval-serve" $PreviousPython
    $McpHealth = $null
    $ApprovalHealth = $null
}

$InstanceNonce = if (-not $ConfigurationChanged -and
    -not [string]::IsNullOrWhiteSpace($SavedNonce) -and
    ($null -ne $McpHealth -or $null -ne $ApprovalHealth)) {
    $SavedNonce
}
else {
    [guid]::NewGuid().ToString("N")
}

if ($null -eq $McpHealth -and (Test-ListeningPort "127.0.0.1" $McpPort)) {
    Stop-VerifiedLegacyJyListener $McpPort "-m jy_agent serve"
}
if ($null -eq $ApprovalHealth -and (Test-ListeningPort $ApprovalBindHost $ApprovalPort)) {
    Stop-VerifiedLegacyJyListener `
        $ApprovalPort "-m jy_agent approval-serve"
}

$env:JY_APPROVAL_TRANSPORT = $ApprovalTransport
$env:JY_APPROVAL_HOST = $ApprovalBindHost
$env:JY_APPROVAL_PORT = [string]$ApprovalPort
$env:JY_APPROVAL_PUBLIC_BASE_URL = $PublicBaseUrl
$env:JY_APPROVAL_ACCESS_TOKEN = $ApprovalAccessToken
$env:JY_APPROVAL_TRUSTED_PROXY_CIDRS = "127.0.0.0/8,::1/128"
$env:JY_SERVICE_INSTANCE_NONCE = $InstanceNonce
$env:JY_RECOVERY_ONLY = if ($RecoveryOnly) { "1" } else { "0" }

if ($null -eq $ApprovalHealth) {
    $ApprovalProcess = Start-Process `
        -FilePath $ResolvedPython `
        -ArgumentList @("-m", "jy_agent", "approval-serve", "--host", $ApprovalBindHost, "--port", [string]$ApprovalPort) `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Runtime "approval.stdout.log") `
        -RedirectStandardError (Join-Path $Runtime "approval.stderr.log")
    $StartedPids += $ApprovalProcess.Id
}
if ($null -eq $McpHealth) {
    $McpProcess = Start-Process `
        -FilePath $ResolvedPython `
        -ArgumentList @("-m", "jy_agent", "serve", "--host", "127.0.0.1", "--port", [string]$McpPort) `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Runtime "mcp.stdout.log") `
        -RedirectStandardError (Join-Path $Runtime "mcp.stderr.log")
    $StartedPids += $McpProcess.Id
}

$Deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
do {
    $McpHealth = Read-Health $McpHealthUri
    $ApprovalHealth = Read-Health $ApprovalHealthUri $ApprovalAccessToken
    if ((Test-HealthIdentity $McpHealth "jy-mcp" $InstanceNonce) -and
        (Test-HealthIdentity $ApprovalHealth "jy-approval" $InstanceNonce)) {
        break
    }
    Start-Sleep -Milliseconds 500
} while ([DateTime]::UtcNow -lt $Deadline)

if (-not (Test-HealthIdentity $McpHealth "jy-mcp" $InstanceNonce) -or
    -not (Test-HealthIdentity $ApprovalHealth "jy-approval" $InstanceNonce)) {
    foreach ($StartedPid in $StartedPids) {
        Stop-Process -Id $StartedPid -ErrorAction SilentlyContinue
    }
    throw "JY services did not become healthy; inspect runtime/*.stderr.log."
}
if ($RemoteProvider -eq "public") {
    $PublicDeadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    do {
        $PublicHealth = Read-Health "$PublicBaseUrl/healthz" $ApprovalAccessToken
        if (Test-HealthIdentity $PublicHealth "jy-approval" $InstanceNonce) {
            break
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $PublicDeadline)
    if (-not (Test-HealthIdentity $PublicHealth "jy-approval" $InstanceNonce)) {
        throw "The public tunnel did not reach the expected JY approval instance."
    }
}

$Bootstrap = [ordered]@{
    python = $ResolvedPython
    qualibrate_config = $ResolvedQualibrateConfig
    remote_provider = $RemoteProvider
    approval_transport = $ApprovalTransport
    approval_bind_host = $ApprovalBindHost
    approval_port = $ApprovalPort
    public_base_url = $PublicBaseUrl
    mcp_endpoint = "http://127.0.0.1:$McpPort/mcp"
    approval_endpoint = if ([string]::IsNullOrWhiteSpace($PublicBaseUrl)) { "http://127.0.0.1:$ApprovalPort" } else { $PublicBaseUrl }
    operator_console = "http://127.0.0.1:$McpPort/operator"
    approval_access_url = $PublicBaseUrl
    approval_access_token_path = $ApprovalAccessTokenPath
    approval_bootstrap_code_path = $ApprovalBootstrapCodePath
    public_tunnel_pid = $PublicTunnelPid
    mcp_pid = [int]$McpHealth.pid
    approval_pid = [int]$ApprovalHealth.pid
    instance_nonce = $InstanceNonce
    recovery_only = $RecoveryOnly
    status = if ($RecoveryOnly) { "recovery_only" } else { "ready" }
    updated_at = [DateTimeOffset]::Now.ToString("o")
}
Write-AtomicJson $BootstrapPath $Bootstrap
Set-PrivateFileAcl $BootstrapPath
[ordered]@{
    status = if ($RecoveryOnly) { "recovery_only" } else { "ready" }
    mcp_endpoint = $Bootstrap.mcp_endpoint
    approval_endpoint = if ($RemoteProvider -eq "public") { "$PublicBaseUrl/?bootstrap_code=$ApprovalBootstrapCode" } else { "http://127.0.0.1:$ApprovalPort" }
    remote_provider = $RemoteProvider
    started_pids = $StartedPids
    mcp_exposed_remotely = $false
    recovery_only = $RecoveryOnly
    operator_console = if ($RecoveryOnly) { "http://127.0.0.1:$McpPort/operator" } else { $null }
} | ConvertTo-Json -Depth 4
}
catch {
    $StartupFailure = $_.Exception.Message
    foreach ($StartedPid in $StartedPids) {
        Stop-Process -Id $StartedPid -ErrorAction SilentlyContinue
    }
    if ($StartedTunnelPid -gt 0) {
        $StartedTunnel = Get-Process -Id $StartedTunnelPid -ErrorAction SilentlyContinue
        if ($null -ne $StartedTunnel -and $StartedTunnel.ProcessName -eq "cloudflared") {
            Stop-Process -Id $StartedTunnelPid -ErrorAction SilentlyContinue
        }
    }
    throw (
        $StartupFailure + " To safely return stale JY workflow/process state " +
        "to a closed state, issue '恢復' or 'Recover' in the repository AI " +
        "conversation. The recovery path will not delete a retained hardware lock."
    )
}
finally {
    if ($LifecycleAcquired) {
        $LifecycleMutex.ReleaseMutex()
    }
    $LifecycleMutex.Dispose()
}
