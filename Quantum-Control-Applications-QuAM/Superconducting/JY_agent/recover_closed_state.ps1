param(
    [Parameter(Mandatory = $false)]
    [string]$Python = $env:JY_QUALIBRATE_PYTHON,
    [Parameter(Mandatory = $false)]
    [string]$QualibrateConfig = $env:QUALIBRATE_CONFIG_FILE,
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$AgentRoot = $PSScriptRoot
$Runtime = Join-Path $AgentRoot "runtime"
$BootstrapPath = Join-Path $Runtime "server-bootstrap.json"
. (Join-Path $AgentRoot "environment.ps1")

function Write-RecoveryResult(
    [string]$Status,
    [string]$MessageZh,
    [string]$MessageEn,
    $Preparation,
    $ServiceStop,
    [string]$ErrorMessage = ""
) {
    [ordered]@{
        status = $Status
        message_zh = $MessageZh
        message_en = $MessageEn
        preparation = $Preparation
        service_stop = $ServiceStop
        error = $ErrorMessage
        recovery_command = [ordered]@{ zh = "恢復"; en = "Recover" }
        completed_at = [DateTimeOffset]::Now.ToString("o")
    } | ConvertTo-Json -Depth 8
}

$Saved = $null
if (Test-Path -LiteralPath $BootstrapPath -PathType Leaf) {
    try {
        $Saved = Get-Content -Raw -LiteralPath $BootstrapPath | ConvertFrom-Json
    }
    catch {
        $Saved = $null
    }
}

$Preparation = $null
$PreparationError = ""
try {
    $Environment = Resolve-JyEnvironment `
        $AgentRoot $Python $QualibrateConfig $Saved -RequireAgent
    $ResolvedPython = [string]$Environment.python
    $PreparationText = (& $ResolvedPython -m jy_agent recover-close `
        --timeout-seconds ([Math]::Max(0, [Math]::Min($TimeoutSeconds, 300))) | Out-String).Trim()
    $PreparationExitCode = $LASTEXITCODE
    if (-not [string]::IsNullOrWhiteSpace($PreparationText)) {
        try {
            $Preparation = $PreparationText | ConvertFrom-Json
        }
        catch {
            $Preparation = [ordered]@{
                status = "unreadable_preparation_result"
                raw = $PreparationText
            }
        }
    }
    if ($PreparationExitCode -ne 0 -or
        ($null -ne $Preparation -and -not [bool]$Preparation.services_may_stop)) {
        Write-RecoveryResult `
            "waiting" `
            "安全收尾已提出，但 worker 尚未離開；網站仍可用，請稍後再輸入「恢復」，或在確認儀器狀態後使用 Dashboard 的停止控制。" `
            "Safe recovery was requested, but the worker has not exited. Keep the Dashboard open and retry ‘Recover’ later, or use its stop controls after checking the instrument." `
            $Preparation $null
        exit 2
    }
}
catch {
    # A broken Qualibrate/JY configuration must not prevent verified service
    # processes from being shut down.  Preserve the configuration error in the
    # result and let stop_server perform its independent identity checks.
    $PreparationError = $_.Exception.Message
}

$StopResult = $null
try {
    $StopText = (& (Join-Path $AgentRoot "stop_server.ps1") | Out-String).Trim()
    if (-not [string]::IsNullOrWhiteSpace($StopText)) {
        $StopResult = $StopText | ConvertFrom-Json
    }
}
catch {
    Write-RecoveryResult `
        "blocked" `
        "系統拒絕關閉未能驗證身分的程序，沒有強制刪除 lock 或終止未知程序。請保留這份錯誤並在量測電腦執行 Doctor。" `
        "The system refused to stop an unverified process. It did not delete a lock or terminate an unknown process. Keep this error and run Doctor on the lab PC." `
        $Preparation $StopResult $_.Exception.Message
    exit 2
}

$LockRetained = [bool]($StopResult.recovery_quarantine) -or
    [bool]($Preparation.hardware_lock_retained)
$Status = if ($LockRetained) { "closed_recovery_required" } elseif (
    -not [string]::IsNullOrWhiteSpace($PreparationError)
) { "closed_configuration_attention" } else { "closed" }
$MessageZh = if ($LockRetained) {
    "所有可驗證的 JY 程式、Dashboard 與 tunnel 已關閉；因硬體狀態無法被軟體證明，lock 僅保留在本機安全隔離，不會讓網站繼續運作。"
} elseif (-not [string]::IsNullOrWhiteSpace($PreparationError)) {
    "所有可驗證的 JY 服務已關閉；啟動設定仍需用 Doctor 檢查後再進入量測模式。"
} else {
    "JY workflow、Dashboard、MCP 與 tunnel 已回到全部關閉狀態；現在可重新進入量測模式。"
}
$MessageEn = if ($LockRetained) {
    "All verified JY programs, Dashboard services, and the tunnel are closed. The lock remains only in local safety quarantine because software could not prove the hardware state; it does not keep the website running."
} elseif (-not [string]::IsNullOrWhiteSpace($PreparationError)) {
    "All verified JY services are closed. Run Doctor to repair the startup configuration before entering measurement mode again."
} else {
    "The JY workflow, Dashboard, MCP service, and tunnel are fully closed. Measurement mode may now be entered again."
}
Write-RecoveryResult $Status $MessageZh $MessageEn $Preparation $StopResult $PreparationError
