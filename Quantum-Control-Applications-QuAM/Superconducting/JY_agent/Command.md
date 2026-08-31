# JY commands / JY 指令

這些是 Codex、Claude Code 與 Cursor 在 repository root 共用的自然語言指令。
英文大小寫以表格中的寫法為準；中文可使用列出的無空格別名。所有量測、停止與
復原仍受 server policy、worker identity 與 hardware-lock 檢查約束。

These natural-language commands are shared by Codex, Claude Code, and Cursor
when the repository root is open. Measurement, stop, and recovery actions remain
subject to server policy, verified worker identity, and hardware-lock checks.

## 對話指令 / Conversation commands

| 用途 / Purpose | 中文 / Chinese | English | 行為 / Behavior |
|---|---|---|---|
| 一般量測 / Conversational measurement | `進入 JY 量測模式`、`進入JY量測模式` | `Enter JY measurement mode` | 啟動或重用服務；每次 run/state change 個別核准。 / Starts or reuses services; every run/state change is approved separately. |
| 有限自動量測 / Bounded automatic measurement | `進入 JY 自動量測模式`、`進入JY自動量測模式` | `Enter JY automatic measurement mode` | 一次核准 8 小時有限 lease，再於核准範圍內自動推進。 / Approves one eight-hour bounded lease, then advances within scope. |
| 喚醒已核准工作 / Wake an approved session | `已核准` | `Approved` | 只在 agent host 暫停回合時使用；它不代替網頁核准。 / Used only when the agent host paused; it never substitutes for Dashboard approval. |
| 暫停自動排程 / Pause automatic scheduling | `暫停 JY 自動量測` | `Pause JY automatic measurement` | 目前 run 完成後不再排新實驗，保留 lease 與網站。 / Stops new scheduling after the current run; keeps lease and site. |
| 繼續自動排程 / Resume automatic scheduling | `繼續 JY 自動量測` | `Resume JY automatic measurement` | 恢復同一份未過期、paused lease。 / Resumes the same unexpired paused lease. |
| 結束自動授權 / End automation only | `結束 JY 自動授權` | `End JY automatic authorization` | 撤銷 lease 並安全停止 active worker，但保留 workflow 與網站。 / Revokes the lease and safely stops an active worker while keeping the workflow and site. |
| 緊急停止 worker / Emergency-stop worker | `緊急停止 JY worker` | `Emergency stop JY worker` | 先協作停止，逾時後只終止驗證過的 worker；網站保留，lock 可能隔離。 / Requests cooperative stop, then terminates only the verified worker after grace; keeps the site and may quarantine the lock. |
| 完整結束 / Full shutdown | `結束量測`、`停止量測`、`退出 JY 量測模式`（以及設定內的 JY 空格別名） | `End measurement`、`Stop measurement`、`Exit JY measurement mode` | 停止 run/workflow，關閉 Dashboard、MCP HTTP service 與 tunnel。 / Stops run/workflow and closes Dashboard, HTTP MCP service, and tunnel. |
| 安全恢復到關閉狀態 / Recover to a closed state | `恢復`、`恢复` | `Recover` | 不依賴已掛載的 JY MCP；收尾 stale workflow/process，再關閉可驗證的 JY 服務。絕不自動刪除 retained hardware lock。 / Does not require the attached JY MCP; converges stale workflow/process state and closes verified JY services. It never auto-deletes a retained lock. |

`停止 JY 自動量測` 屬於完整結束語句；若只想保留網站，請使用「結束 JY 自動授權」
或 Results & controls 頁面的對應按鈕。

`停止 JY 自動量測` is a full-shutdown phrase. To keep the site, use
`End JY automatic authorization` or the matching Results & controls button.

## Dashboard 控制 / Dashboard controls

- Home：`結束量測並關閉所有 JY 服務`，需輸入頁面顯示的
  `SHUTDOWN <session-id>`。
- Approval：核准待處理的 run、state change 或初始 bounded lease。
- Results & controls：Pause、Resume、End automation（保留網站）、Emergency
  stop worker（保留網站），以及該次服務的完整實驗下拉歷史。

- Home: full shutdown, protected by the displayed `SHUTDOWN <session-id>` text.
- Approval: approves a pending run, state change, or initial bounded lease.
- Results & controls: Pause, Resume, End automation (keep site), Emergency stop
  worker (keep site), and the service-lifetime experiment history.

## 一次性維護指令 / One-line maintenance commands

正常使用不必開終端機；下列是本機安裝或排錯 fallback，均從 repository root 執行。
Normal use does not require a terminal. These are local installation/debug
fallbacks, run from the repository root.

```powershell
# 一次安裝 / one-time install
.\jy_agent.ps1 -Action Install

# MCP/client 設定與真實 handshake 診斷 / diagnose client config and handshake
.\jy_agent.ps1 -Action Doctor

# 啟動或重用本機服務（通常由 agent 自動執行） / ensure services (normally automatic)
.\jy_agent.ps1 -Action Ensure

# 完整停止服務 / stop services
.\jy_agent.ps1 -Action Stop

# 與自然語言「恢復 / Recover」相同的安全收尾 / safe close-state recovery
.\jy_agent.ps1 -Action Recover

# 僅限量測電腦、實體確認後的 retained-lock recovery
# retained-lock recovery only after local physical inspection
.\jy_agent.ps1 -Action RecoverLock -RunId <run-id>
```

若 AI App 斷線但 Dashboard 仍可用，優先用 Dashboard 暫停或完整關機。網路恢復後
可在原對話輸入 `結束量測`／`End measurement` 再確認一次。若重新進入因殘留服務、
workflow 或設定失敗，輸入 `恢復`／`Recover`；只有軟體無法證明硬體 safe state 的
情況才會留下 local recovery quarantine。

If the AI app disconnects while the Dashboard remains reachable, use Dashboard
pause or full shutdown. After connectivity returns, send `End measurement` in
the original conversation to verify closure. If a later entry fails because of
stale services, workflow state, or configuration, send `Recover`. Local recovery
quarantine remains only when software cannot prove the hardware safe state.
