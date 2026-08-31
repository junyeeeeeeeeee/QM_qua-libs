param(
    [int]$McpPort = 8765,
    [int]$ApprovalPort = 8766,
    [string]$SessionId = ""
)

$ErrorActionPreference = "Stop"
$AgentRoot = $PSScriptRoot
$Runtime = Join-Path $AgentRoot "runtime"
$BootstrapPath = Join-Path $Runtime "server-bootstrap.json"
$TokenPath = Join-Path $Runtime "public-dashboard-access.token"
$BootstrapCodePath = Join-Path $Runtime "public-dashboard-bootstrap.json"
if (-not [string]::IsNullOrWhiteSpace($SessionId) -and
    $SessionId -notmatch '^[a-fA-F0-9]{32}$') {
    throw "SessionId must be a 32-character hexadecimal JY session id."
}

function Write-AtomicJson([string]$Path, $Value) {
    $Directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $Temporary = Join-Path $Directory ("." + [IO.Path]::GetFileName($Path) + "." + [guid]::NewGuid().ToString("N") + ".tmp")
    $Backup = Join-Path $Directory ("." + [IO.Path]::GetFileName($Path) + "." + [guid]::NewGuid().ToString("N") + ".bak")
    $Utf8NoBom = New-Object Text.UTF8Encoding($false)
    try {
        [IO.File]::WriteAllText(
            $Temporary,
            (($Value | ConvertTo-Json -Depth 8) + "`n"),
            $Utf8NoBom
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

function Test-ListeningPort([string]$HostAddress, [int]$Port) {
    $Client = New-Object Net.Sockets.TcpClient
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

function Stop-ManagedJyService(
    $Health,
    [int]$BootstrapPid,
    [string]$ExpectedService,
    [string]$ExpectedNonce,
    [string]$ExpectedCommandFragment,
    [string]$ExpectedPython
) {
    if ($null -ne $Health) {
        if ([string]$Health.service -ne $ExpectedService -or
            [string]$Health.instance_nonce -ne $ExpectedNonce) {
            throw "Refusing to trust an unidentified listener; expected $ExpectedService."
        }
        if ($BootstrapPid -gt 0 -and [int]$Health.pid -ne $BootstrapPid) {
            throw "Health PID does not match the atomic JY bootstrap record."
        }
        $ServicePid = [int]$Health.pid
    }
    else {
        $ServicePid = $BootstrapPid
    }
    if ($ServicePid -le 0) {
        return $null
    }
    $Process = Get-Process -Id $ServicePid -ErrorAction SilentlyContinue
    if ($null -eq $Process) {
        return $null
    }
    $Info = Get-CimInstance Win32_Process -Filter "ProcessId = $ServicePid" `
        -ErrorAction SilentlyContinue
    $CommandLine = [string]$Info.CommandLine
    $Executable = [string]$Info.ExecutablePath
    if ($null -eq $Info -or $CommandLine -notlike "*$ExpectedCommandFragment*" -or
        [string]::IsNullOrWhiteSpace($Executable) -or
        -not [IO.Path]::GetFullPath($Executable).Equals(
            [IO.Path]::GetFullPath($ExpectedPython),
            [StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Refusing to stop PID $ServicePid because its exact JY identity failed."
    }
    Stop-Process -Id $ServicePid
    $Process.WaitForExit(5000) | Out-Null
    if ($null -ne (Get-Process -Id $ServicePid -ErrorAction SilentlyContinue)) {
        throw "Managed JY PID $ServicePid did not stop within five seconds."
    }
    return $ServicePid
}

function Stop-ManagedTunnel($Bootstrap, [int]$Port) {
    $TunnelPid = [int]$Bootstrap.public_tunnel_pid
    if ($TunnelPid -le 0) {
        return $null
    }
    $Process = Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue
    if ($null -eq $Process) {
        return $null
    }
    $Info = Get-CimInstance Win32_Process -Filter "ProcessId = $TunnelPid" `
        -ErrorAction SilentlyContinue
    $CommandLine = [string]$Info.CommandLine
    $ExpectedTarget = "http://127.0.0.1:$Port"
    if ($Process.ProcessName -ne "cloudflared" -or $null -eq $Info -or
        $CommandLine -notlike "*tunnel*" -or
        $CommandLine -notlike "*$ExpectedTarget*") {
        throw "Refusing to stop an unverified public tunnel process."
    }
    Stop-Process -Id $TunnelPid
    $Process.WaitForExit(5000) | Out-Null
    if ($null -ne (Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue)) {
        throw "The public tunnel did not stop within five seconds."
    }
    return $TunnelPid
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
        throw "Unable to verify whether a JY worker is alive; refusing shutdown."
    }
}

$MutexHasher = [Security.Cryptography.SHA256]::Create()
try {
    $MutexHash = $MutexHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($AgentRoot.ToLowerInvariant()))
}
catch {
    if (-not [string]::IsNullOrWhiteSpace($SessionId)) {
        Write-AtomicJson (Join-Path $Runtime "shutdown-stop-failures\$SessionId.json") ([ordered]@{
            session_id = $SessionId
            failed_at = [DateTimeOffset]::Now.ToString("o")
            error = $_.Exception.Message
        })
    }
    throw
}
finally {
    $MutexHasher.Dispose()
}
$MutexSuffix = (([BitConverter]::ToString($MutexHash) -replace "-", "").Substring(0, 24))
$LifecycleMutex = New-Object Threading.Mutex($false, "Local\JYAgentLifecycle-$MutexSuffix")
$LifecycleAcquired = $false
try {
    $LifecycleAcquired = $LifecycleMutex.WaitOne([TimeSpan]::FromSeconds(60))
    if (-not $LifecycleAcquired) {
        throw "Timed out waiting for the JY lifecycle lock."
    }

    $Bootstrap = $null
    if (Test-Path -LiteralPath $BootstrapPath -PathType Leaf) {
        try {
            $Bootstrap = Get-Content -Raw -LiteralPath $BootstrapPath | ConvertFrom-Json
        }
        catch {
            throw "The JY bootstrap record is unreadable; refusing an unverified shutdown."
        }
    }

    if ($null -ne $Bootstrap) {
        if ([int]$Bootstrap.approval_port -gt 0) {
            $ApprovalPort = [int]$Bootstrap.approval_port
        }
        try {
            $Endpoint = [Uri]([string]$Bootstrap.mcp_endpoint)
            if ($Endpoint.IsLoopback -and $Endpoint.Port -gt 0) {
                $McpPort = $Endpoint.Port
            }
        }
        catch {
            throw "The bootstrap MCP endpoint is invalid."
        }
    }

    $HealthToken = ""
    if (Test-Path -LiteralPath $TokenPath -PathType Leaf) {
        $HealthToken = (Get-Content -Raw -LiteralPath $TokenPath).Trim()
    }
    $McpHealth = Read-Health "http://127.0.0.1:$McpPort/healthz"
    $ApprovalHealth = Read-Health "http://127.0.0.1:$ApprovalPort/healthz" $HealthToken
    $HardwareLock = Join-Path $Runtime "hardware.lock"
    $RecoveryMarker = Join-Path $Runtime "recovery-required.json"
    $RecoveryQuarantine = $false
    if ($null -ne $McpHealth -and [bool]$McpHealth.active_run) {
        throw (
            "A JY hardware run is still active. Stop it through JY, poll it to a " +
            "terminal state, and stop the workflow before shutting down services."
        )
    }
    if (Test-Path -LiteralPath $HardwareLock -PathType Leaf) {
        try {
            $LockRecord = Get-Content -Raw -LiteralPath $HardwareLock | ConvertFrom-Json
        }
        catch {
            throw "The retained JY hardware lock is unreadable; refusing unverified quarantine."
        }
        if ([string]::IsNullOrWhiteSpace([string]$LockRecord.run_id)) {
            throw "The retained JY hardware lock has no run id; refusing unverified quarantine."
        }
        $LiveWorkers = @(Get-LiveJyWorkers $AgentRoot)
        if ($LiveWorkers.Count -gt 0) {
            $WorkerIds = ($LiveWorkers | ForEach-Object { [string]$_.ProcessId }) -join ", "
            throw "JY worker PID(s) $WorkerIds are still alive; refusing to close their control services."
        }
        $RecoveryQuarantine = $true
        Write-AtomicJson $RecoveryMarker ([ordered]@{
            status = "recovery_required"
            run_id = [string]$LockRecord.run_id
            hardware_lock_path = $HardwareLock
            services_may_stop = $true
            created_at = [DateTimeOffset]::Now.ToString("o")
            reason = "No live JY worker remains, but its hardware lock was retained for local recovery."
        })
    }

    if ($null -eq $Bootstrap -and
        ((Test-ListeningPort "127.0.0.1" $McpPort) -or
         (Test-ListeningPort "127.0.0.1" $ApprovalPort))) {
        throw "JY ports are open but no trusted bootstrap record exists."
    }

    $StoppedPids = @()
    if ($null -ne $Bootstrap) {
        $TunnelPid = Stop-ManagedTunnel $Bootstrap $ApprovalPort
        if ($null -ne $TunnelPid) {
            $StoppedPids += $TunnelPid
        }
        $Nonce = [string]$Bootstrap.instance_nonce
        $Python = [string]$Bootstrap.python
        $ManagedServiceExpected = (
            $null -ne $ApprovalHealth -or $null -ne $McpHealth -or
            [int]$Bootstrap.approval_pid -gt 0 -or
            [int]$Bootstrap.mcp_pid -gt 0
        )
        if ($ManagedServiceExpected -and
            ([string]::IsNullOrWhiteSpace($Nonce) -or
             [string]::IsNullOrWhiteSpace($Python))) {
            throw "Bootstrap identity metadata is incomplete; refusing shutdown."
        }
        if ($ManagedServiceExpected) {
            $ApprovalPid = Stop-ManagedJyService $ApprovalHealth `
                ([int]$Bootstrap.approval_pid) "jy-approval" $Nonce `
                "-m jy_agent approval-serve" $Python
            if ($null -ne $ApprovalPid) {
                $StoppedPids += $ApprovalPid
            }
            $McpPid = Stop-ManagedJyService $McpHealth ([int]$Bootstrap.mcp_pid) `
                "jy-mcp" $Nonce "-m jy_agent serve" $Python
            if ($null -ne $McpPid) {
                $StoppedPids += $McpPid
            }
        }
    }

    if ((Test-ListeningPort "127.0.0.1" $McpPort) -or
        (Test-ListeningPort "127.0.0.1" $ApprovalPort)) {
        throw "A JY port remains open; shutdown is not complete."
    }

    $AccessTokenRemoved = $false
    foreach ($SecretPath in @($TokenPath, $BootstrapCodePath)) {
        if (Test-Path -LiteralPath $SecretPath -PathType Leaf) {
            Remove-Item -LiteralPath $SecretPath -Force
            $AccessTokenRemoved = $true
        }
    }

    if ($null -ne $Bootstrap) {
        $Bootstrap.public_tunnel_pid = 0
        $Bootstrap.mcp_pid = 0
        $Bootstrap.approval_pid = 0
        $Bootstrap | Add-Member -NotePropertyName public_base_url -NotePropertyValue "" -Force
        $Bootstrap | Add-Member -NotePropertyName approval_access_url -NotePropertyValue "" -Force
        $Bootstrap | Add-Member -NotePropertyName approval_access_token_path -NotePropertyValue "" -Force
        $Bootstrap | Add-Member -NotePropertyName approval_bootstrap_code_path -NotePropertyValue "" -Force
        $Bootstrap | Add-Member -NotePropertyName instance_nonce -NotePropertyValue "" -Force
        $Bootstrap | Add-Member -NotePropertyName status `
            -NotePropertyValue $(if ($RecoveryQuarantine) { "stopped_recovery_required" } else { "stopped" }) -Force
        $Bootstrap | Add-Member -NotePropertyName stopped_at `
            -NotePropertyValue ([DateTimeOffset]::Now.ToString("o")) -Force
        Write-AtomicJson $BootstrapPath $Bootstrap
    }

    [ordered]@{
        status = "stopped"
        stopped_pids = @($StoppedPids | Sort-Object -Unique)
        mcp_endpoint = "http://127.0.0.1:$McpPort/mcp"
        approval_endpoint = "http://127.0.0.1:$ApprovalPort"
        public_tunnel_stopped = $true
        dashboard_access_token_removed = $AccessTokenRemoved
        recovery_quarantine = $RecoveryQuarantine
        hardware_lock_retained = $RecoveryQuarantine
    } | ConvertTo-Json -Depth 4
}
finally {
    if ($LifecycleAcquired) {
        $LifecycleMutex.ReleaseMutex()
    }
    $LifecycleMutex.Dispose()
}
