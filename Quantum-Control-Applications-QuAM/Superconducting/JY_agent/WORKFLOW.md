# JY_agent workflow / 工作流程

## 中文

### 0. 一次性安裝

1. 確認 Qualibrate、`~/.qualibrate/config.toml`、QuAM state/wiring、Data 與
   calibration library 可正常使用。
2. 在 repository 最上層執行 `powershell -ExecutionPolicy Bypass -File
   .\jy_agent.ps1 -Action Install`。
   也可要求一般 coding agent 執行同一指令；這一步不需要 JY MCP，也不開始量測。
3. 用 Codex、Claude Code 或 Cursor 開啟 repository 最上層為 project/workspace，
   接受一次性的 MCP trust 提示。最外層資料夾名稱可變，且不需另開 `JY_agent`。

### 1. Client bootstrap

Client 讀取 repository-root project config，啟動 `jy_agent.ps1 -Action Stdio`；
root shim 再以相對路徑定位並啟動 `JY_agent/start_mcp_stdio.ps1`。此流程：

1. 尋找已安裝的 Qualibrate Python。
2. 啟動 client-owned STDIO MCP protocol，讓 client 取得 `jy_*` tools。
3. STDIO 的 stdout 只傳 MCP frame；不啟動網站、tunnel、port，也不依賴網路。

沒有這一步，agent 尚未有 `jy_*` tools，因此自然語言本身無法從不存在的 MCP
啟動 MCP。這就是 bootstrap 的雞生蛋邊界。開啟 project 只建立工具，不會建立
run、啟動公開網站、接觸儀器或修改 state。

收到任一進入語句後，agent 才以一般 shell 能力執行 idempotent
`jy_agent.ps1 -Action Ensure`：啟動／沿用 loopback MCP HTTP service
(`127.0.0.1:8765`) 與 Approval service (`127.0.0.1:8766`)，建立或重建只 forward
Dashboard port 的 Cloudflare quick tunnel 和 256-bit access token，並將 public URL、
PID、port 與 Python 路徑記錄到 `runtime/server-bootstrap.json`。長駐 STDIO service
會動態讀取這份 metadata，因此不必為了取得新 Dashboard URL 重啟 MCP。最後 agent
才呼叫對應的 `jy_enter_*` tool。

### 2. 進入模式

完整中英文自然語言與本機維護指令見 [Command.md](Command.md)。英文入口為
`Enter JY measurement mode` 與 `Enter JY automatic measurement mode`。

- `進入 JY 量測模式`：agent 先檢查服務健康度，再呼叫
  `jy_enter_measurement_mode`。使用者取得一個共用 Dashboard URL；每次透過
  `jy_request_conversational_run` 建立單一提案、網頁核准、執行、snapshot 分析、
  Decision/Reason/Next action，state commit 另行核准。
- `進入 JY 自動量測模式`：agent 呼叫 `jy_enter_autonomy_mode`，使用者在 Dashboard
  的核准分頁核准一次 bounded lease。之後只用 `jy_autonomy_*` tools；超出 targets、nodes、
  8 小時、20 次／node／qubit 或參數安全範圍必須重新授權。Codex 若提供 durable
  goal，會建立 session-scoped goal，並用 `jy_wait_for_autonomy_status` 等待事件；
  Claude/Cursor 的 host 若結束 turn，需以 Dashboard 的 `已核准` 提示續跑。
- 對使用者只呈現 entry response 最外層的 `browser_url`。這個 `/` 是共用入口，
  會連到同一 session 的首頁、核准、結果與控制分頁；舊 `/approve/...`、
  `/autonomy/...` 與 `/session/...` 路徑只做 303 redirect。
  直接呼叫 lease request 或替換停止過的 lease 時，service 仍必須重用並重新綁定
  同一個 workflow session。

### 3. 每次實驗的資料流

1. `PolicyEngine` 將 defaults 與提案參數合併並驗證 hard policy。
2. Worker 在 lock 與 subprocess 邊界執行已登錄 node；agent 不直接跑 script。
   停止時 runner 寫入含 run/process token 的 request，worker 協作式停止並寫 exit
   receipt；不再依賴 Windows console `CTRL_BREAK_EVENT`。
3. 完成後保存 snapshot、完整參數、開始／結束／耗時及狀態。
4. `SnapshotAnalyzer` 讀 `node.json`、`data.json`、`ds.h5` 與結果圖，獨立驗證
   fitting、SNR、coverage 與 node-specific 規則；node outcome 只是 advisory。
5. 決策與原因存入 SQLite，Dashboard 收到 SSE event 後更新；舊結果保留。
6. 只有 `pass` 且 decision patch 完全一致的自動流程可進入 state commit；
   hash conflict、snapshot 遺失、worker/lock 異常或 policy violation 立即停排程。

儀器 transport 例外是獨立的 pause 分支：run 會記錄
`instrument_unreachable`，一般 workflow 或 bounded lease 轉為 `paused`，Dashboard
顯示恢復方式。可證明連線從未建立且 state 已恢復時釋放 lock；執行中失聯則保留
local quarantine，但不阻擋完整關機關閉服務。

`02x`、`02a` 是 fixed multiplex nodes。bounded lease 必須涵蓋 workflow 的全部
targets；若 Decision 只指出 q2 需要重測，server 仍會把 02a 的 `qubits` 正規化成
完整 q1–q10 batch。子群 lease 會在碰硬體前被拒絕，但不會因此誤把 lease halt。

預設流程在 `06` 後立刻執行 `06b`。所有有 reset 參數的實驗預設使用 thermal；
只有 active-reset 07b 同時通過緊實雙雲團判斷且 fidelity `>= 0.85` 的 qubit，後續
才可明確改用 active。若統計實驗要用 active，必須先用 active 重跑並接受 05、06、
06b。三個基準實驗均須至少涵蓋 3.5 lifetimes，統計時間窗約為對應 lifetime 的
3.5–5.5 倍（建議 4.5 倍）。前置條件通過後，05st 與兩個 06st 各跑一次即可；
JY 不再逐筆重 fitting 100 個 iteration，也沒有額外的 100/100 否決門檻。

### 4. Dashboard 與停止

Dashboard 右上角可選 `繁體中文` 或 `English`。Prediction、Parameters、Analysis、
Next action 都是摺疊面板；Decision 與 Reason 直接顯示。只有新結果或控制事件才
更新，不做固定秒數刷新。首頁只顯示工作階段狀態、裝置配對與完整關機；核准頁
只在 proposal 事件更新；結果頁保留所有實驗並提供四種 operator 狀態：暫停目前
實驗後的排程、繼續同一份未過期 lease、結束自動授權但保留網站、緊急停止 worker
但保留網站。

`結束量測`、`停止量測`、`退出 JY 量測模式` 等設定語句，或 Dashboard 的
首頁的「完整結束量測」按鈕（需輸入 `SHUTDOWN <session-id>`），會先持久化每個
session 的 shutdown request，再安全停止／等待 active run，接著永久停止 workflow，
再由本機 coordinator 執行 `stop_server.ps1`。這個按鈕與 authorization 的停止
不同，而且 lease 已 `halted` 時仍會顯示。若 helper 啟動失敗，頁面會顯示原因並
保留重試按鈕。

AI App／外部網路斷線不會讓 Dashboard 控制失效。網路恢復後可輸入
`結束量測`／`End measurement`；若下一次 entry 被殘留 process、workflow 或
lifecycle state 阻擋，輸入 `恢復`／`Recover`。Agent 會執行 root
`jy_agent.ps1 -Action Recover` 收斂到 closed state；這條路徑不需 MCP attachment，
且不會自動刪除 retained lock 或終止未驗證的 process。

若失敗 run 刻意保留 hardware lock，coordinator 會先確認沒有任何 JY worker 活著，
然後把 workflow 標成 `recovery_required`、保留 lock 與 marker，但仍關閉 MCP、
Dashboard 與 tunnel。下次 `Ensure` 強制為 loopback recovery-only；現場 operator
查看 QOP/OPX 與相連儀器並確認沒有 active job/output 後，再於量測電腦開啟
`http://127.0.0.1:8765/operator`。頁面會顯示所有檢查、實體確認 checkbox 與完整
確認句；成功後會封存 lock、留下 receipt，之後才可重新進入量測模式。
本機 terminal 仍保留為 fallback：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\jy_agent.ps1 `
  -Action RecoverLock -RunId <run-id>
```

此流程會重新檢查 worker、request identity 與 state/recovery hashes；只有全數通過，
且 operator 輸入畫面顯示的完整確認句後，才會封存 lock 並留下 audit receipt。不得
手動刪除 `runtime\hardware.lock`。

手機或第二個瀏覽器不能重播桌面已使用的初次 Dashboard URL。請在同一個本機
operator console 為每台裝置建立一條 10 分鐘、一次性的配對連結。每台裝置使用
獨立的 12 小時 secure cookie，可個別撤銷。

## English

### 0. One-time installation

1. Verify that Qualibrate, `~/.qualibrate/config.toml`, QuAM state/wiring,
   Data, and the calibration library work.
2. From the repository root, run `powershell -ExecutionPolicy Bypass -File
   .\jy_agent.ps1 -Action Install`.
   An ordinary coding agent can run the same command; JY MCP is not needed and
   no measurement starts.
3. Open the repository root as the Codex, Claude Code, or Cursor workspace and
   accept the one-time project MCP trust prompt. The outer folder may be renamed;
   opening the nested `JY_agent` folder is not required.

### 1. Client bootstrap

The client reads its repository-root project config and launches
`jy_agent.ps1 -Action Stdio`. That name-independent root shim resolves the nested
`JY_agent/start_mcp_stdio.ps1`. The launcher discovers Qualibrate Python and
starts only the client-owned STDIO MCP protocol. Its stdout carries MCP frames
only; website, tunnel, port, and network startup are deliberately excluded.
Opening the project creates tools only; it does not start a public site, create a
run, touch instruments, or mutate state.

After an entry phrase, the agent runs the idempotent root
`jy_agent.ps1 -Action Ensure` using ordinary shell access. Ensure starts or
reuses the loopback MCP HTTP and Approval services, forwards only the Approval
port through a Cloudflare quick tunnel, creates a 256-bit access token, and
records verified URL/process/port metadata. The already-running STDIO service
reads that metadata dynamically, so it need not restart before the selected
`jy_enter_*` tool is called. A website or tunnel failure therefore cannot close
the MCP connection before its `initialize` response.

### 2. Enter a mode

- `進入 JY 量測模式` calls `jy_enter_measurement_mode`. One shared Dashboard
  handles per-run and per-state-change approval.
- `進入 JY 自動量測模式` calls `jy_enter_autonomy_mode`. One Dashboard approval
  activates a bounded lease; later actions must remain within targets, nodes,
  eight hours, 20 attempts per node/qubit, and hard parameter policy. Codex uses
  a session-scoped durable goal when available and waits with
  `jy_wait_for_autonomy_status`; Claude/Cursor require a host resume if their turn ends.
- Surface only the entry response's top-level `browser_url`. The common `/`
  entry links to Home, Approval, and Results & controls for one session; legacy
  approval/control/session paths issue a 303 redirect. Direct and
  replacement lease requests must rebind the workflow's stable session.

### 3. Per-experiment data flow

The policy engine validates merged parameters. A lock-protected worker runs only
a registered node. Stop delivery uses an authenticated request file and a worker
exit receipt rather than Windows console signaling. Snapshot artifacts, parameters, timing, and statuses are
recorded. The independent analyzer rechecks raw data, fitting, SNR, coverage, and
node rules. Decision, Reason, and Next action are appended to SQLite and the
Dashboard updates on an SSE event. Only a passing, identical decision patch can
be committed automatically; snapshot, worker, hash, lock, and policy anomalies
halt scheduling immediately.

The default sequence runs 06b immediately after 06. Every reset-capable node
defaults to thermal reset. A qubit may explicitly switch downstream runs to active
reset only after an active-reset 07b run shows two compact clouds and fidelity of
at least 0.85. Active-reset statistics require accepted active-reset repeats of
05, 06, and 06b first. Those baselines must cover at least 3.5 lifetimes, and the
statistics window remains 3.5–5.5 times the corresponding lifetime (4.5x is
recommended). After the prerequisites pass, each statistics node runs once; JY
does not refit every raw iteration or impose an additional 100/100 rejection gate.

### 4. Dashboard and shutdown

The language selector switches the entire UI. Prediction, Parameters, Analysis,
and Next action are collapsed; Decision and Reason stay visible. Updates occur
only on relevant events. Home contains status, pairing, and full shutdown;
Approval contains pending authorization; Results retains every experiment and
provides Pause, Resume, End automation while keeping the site, and Emergency-stop
worker while keeping the site. Full shutdown requires the exact
`SHUTDOWN <session-id>` confirmation. Each session persists its own shutdown
request before the worker is touched. Configured exit phrases safely stop or
poll an active run, leave/quarantine the workflow, and run `stop_server.ps1`.
A retained hardware lock is preserved but, after every JY worker is verified
dead, no longer keeps the services or tunnel running. The next Ensure is
loopback recovery-only. The same full-shutdown action is present
on a halted Dashboard. A verified recovery can then be completed from the lab PC's
loopback-only `/operator` page; the terminal `RecoverLock` action remains a
fallback. Phones and secondary browsers use separate ten-minute pairing links
created on that page, not a replay of the initial Dashboard URL.
