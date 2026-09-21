# Update log / 更新紀錄

## 2026-09-20

- Dashboard 版面調整：暫停排程、繼續排程、結束自動授權與緊急停止 worker 從
  「結果與控制」頁移到首頁，排在狀態卡下方、完整關機卡上方；第三個分頁因此
  改名為「實驗結果」。結果頁最上方新增「結果摘要」：依序列出本次服務的
  `實驗N: <node>`，只有在 JY 判定該 run 通過、且節點回報該 qubit 為 successful
  時，才在右側寫出成功的 qubit 並附上該次實驗的結果圖縮圖（最多 6 張，其餘
  以數量標註）。未成功或進行中的實驗仍會列出，但右側不放 qubit 與圖。點任一
  列或縮圖會跳到下方該次實驗的完整面板。
- Dashboard layout: pause, resume, end-automation, and emergency-stop-worker
  moved from the results page to Home, between the status card and full
  shutdown, so the third tab is now named Experiment results. The results page
  opens with a new result summary that lists `Experiment N: <node>` in order.
  Successful qubits and result-plot thumbnails (capped at six, with the
  remainder noted) appear only when JY passed the run and the node also
  reported that qubit as successful. Unsuccessful and in-progress experiments
  keep their row without qubits or plots. Selecting a row or thumbnail opens
  that experiment's full panel below.

## 2026-09-06

- 取消 `02x`/`02a` fixed full-batch retry：與其他節點相同，第一次全目標 multiplex，
  之後只重測 unresolved 子群。誤排已通過目標改為 soft-reject，不再 hard-stop lease。
- Removed fixed full-batch retries for `02x`/`02a`. Every node now follows
  first-batch-all-targets then unresolved-subgroup retries. Re-including
  resolved targets is rejected without halting the autonomy lease.

## 2026-09-04

- 所有節點改為 02x 式 multiplex-first：第一次 run 必須用同一組參數量測全部
  active targets。只有部分 qubit 失敗時，才把那些未通過的 qubit 成組改參數
  重測；不可一開始就逐顆孤立量測。`02c`/`04`/`05`/`07b`/`06`/`06b`/`10a`
  由 server 強制第一輪全顆 multiplex。
- All nodes now follow the 02x multiplex-first schedule. The first run must
  measure every active target with one shared parameter set. Retries may omit
  resolved qubits, but unresolved qubits that can share a parameter change are
  grouped. Sequential isolated one-qubit starts are no longer the default.

## 2026-09-02

- Resonator 策略改為：操作者先把足夠正確的 resonator RF 寫入 `state.json`；
  `02x`/`02c`/`02a` 只做數 MHz 級微調。`frequency_span_in_mhz` 上限 60 MHz，
  且掃描窗口不可包含 readout `upconverter_frequency`，因為原始 fitting 會把
  LO（IF=0）誤認為共振。預設順序改為 `02x → 02c → 02a → …`；`02a` 只做
  dressed frequency 小範圍掃描。
- Resonator strategy: operator-supplied RF in `state.json` is already close.
  `02x`/`02c`/`02a` only fine-tune a few MHz, cap span at 60 MHz, and reject
  windows that include the readout upconverter. Sequence is now
  `02x → 02c → 02a → …`; `02a` is a simple dressed-frequency scan.

- 進入語句不必再寫 `target:` 或 `multiplex:`。新 workflow 從 `state.json` 的
  `active_qubit_names` 取 targets，`multiplexed` 預設為 true；明確覆寫仍可用。
  既有非 multiplex workflow 以 phrase-only 恢復時，不會被預設值改成 multiplex。
- Phrase-only entry no longer requires chat-side `target` or `multiplex`. New
  workflows use `state.json` `active_qubit_names` and default `multiplexed=true`.
  Resuming a non-multiplexed workflow with an omitted multiplexed flag keeps it
  unchanged.

## 2026-08-29

- 修正完整關機順序：先寫 SQLite 與
  `runtime/shutdown_requests/<session-id>.json`，再向 worker 寫 authenticated stop
  request；worker 留下 exit receipt 與 termination cause，不再使用 Windows
  `CTRL_BREAK_EVENT`。Grace 逾時後仍只可終止驗證過的 worker PID。
- retained hardware lock 改為 recovery quarantine：所有 worker 死亡後仍保留 lock
  與 audit marker，但 `stop_server.ps1` 會關閉 Dashboard、MCP、Cloudflare tunnel
  並撤銷 secret；下次 Ensure 僅啟動 loopback recovery-only 服務。
- 核准成功訊息依模式分流：一般模式顯示回對話輸入 `已核准`；自動模式直接續跑，
  並把同一句保留為 host 暫停時的喚醒 fallback。新增
  `jy_wait_for_autonomy_status` 與 Codex session-scoped durable-goal contract。
- 公開 `/` 改為手機與電腦的共同 Dashboard 入口，並將同一 session 分為首頁、
  核准、結果與控制三頁；舊 `/session/...`、`/approve/...`、`/autonomy/...` 保留
  303 相容導向。
- SSE 更新依頁面分流：核准頁只追 proposal，結果頁只追 run／decision／control，
  首頁追工作階段狀態；結果下拉歷史仍保留到完整關機。
- 結果頁補齊 Resume，並清楚區分 Pause、End automation（保留網站）、Emergency
  stop worker（保留網站）。首頁完整關機新增 `SHUTDOWN <session-id>` 二次確認。
- system-level 單一控制 Agent ownership lease 列入 TODO；本次未宣稱現有 DB／lock
  已能防止兩個不同 AI controller 同時發出不同有效動作。

## 2026-08-30

- 新增儀器連線例外分類：QOP/OPX/Octave/transport 無法連線時不再當成一般 crash，
  而是暫停 workflow／bounded lease；Dashboard 顯示雙語原因與恢復步驟。只有可證明
  連線從未建立、state hash 已恢復的情況才自動釋放 lock，中途失聯仍保留本機隔離。
- 新增自然語言 `恢復`／`Recover` 與 root `jy_agent.ps1 -Action Recover`。它不依賴
  MCP attachment，可協作停止 verified worker、收斂 stale workflow/lease/session，
  並關閉 verified Dashboard/MCP/tunnel；不刪 retained lock、不終止未知程序。
- 新增 [Command.md](Command.md)，統一 Codex、Claude、Cursor 的中英文進入、核准
  喚醒、暫停、繼續、自動授權停止、緊急停止、完整關機與 recovery 指令。
- Dashboard Home 加入 AI／網路斷線自助處置；網路恢復後可輸入 `結束量測`／
  `End measurement`，下次 entry 被 stale state 阻擋時可輸入 `恢復`／`Recover`。

- Worker 現在會在載入或執行量測節點之前，先把 active-state pre-run SHA-256 與
  recovery copy 路徑寫入 SQLite/audit；即使程序之後被作業系統直接終止，正式
  hardware-lock recovery 也不會因缺少 DB checkpoint 而成為無法完成的死路。
- 為既有舊版 abrupt-exit lock 加入嚴格相容復原：只有 run/request/lock identity、
  terminal/worker 檢查都通過，且 configured active state、run active state 與受保護
  recovery backup 完全同 hash 時，才顯示現場 recovery form。仍需實體檢查、操作人
  身分與完整確認句；不會自動刪除 lock。
- 修正 recovery-only 的暫時 `remote_provider=local` 被下一次正常 Ensure 當成永久
  偏好重用。Retained lock 存在時仍只允許 loopback recovery；正式復原後的正常進入
  預設恢復 token-protected Cloudflare Dashboard，且 recovery mode 改變會強制重啟
  MCP/Dashboard process 以載入正確環境。

## 2026-08-29 (English)

- Persisted each full-shutdown request before worker stop delivery. Workers now
  consume authenticated stop files and write exit receipts/termination causes;
  Windows `CTRL_BREAK_EVENT` is no longer used.
- Retained locks now enter recovery quarantine after every worker exits. Services,
  tunnel, and secrets still close; the next Ensure is loopback recovery-only.
- Added mode-aware post-approval text, `jy_wait_for_autonomy_status`, and the
  Codex session-scoped durable-goal contract with `已核准` as a host-resume fallback.
- Made `/` the common phone/desktop entry and split one session into Home,
  Approval, and Results & controls. Legacy routes remain compatibility redirects.
- Added page-specific event refresh, browser Resume, explicit keep-site semantics
  for automation stop/emergency stop, and an exact second confirmation for full
  shutdown. Added system-level single-controller ownership to TODO only.

## 2026-08-28

- 修正 02a edge-feature retry 與 fixed multiplex workflow 衝突：完整 bounded lease
  會把單一 qubit retry 正規化成完整 workflow targets；子群 lease 在硬體啟動前
  拒絕，不再把既有 lease 誤標成 halted。
- 公開 Dashboard 新增一次性 10 分鐘裝置配對與每裝置 12 小時 secure cookie；
  桌面、手機憑證彼此獨立，可從本機 operator console 個別撤銷。
- halted Dashboard 新增獨立「完整結束量測」控制；安全等待 active run 後停止
  workflow，延遲呼叫受驗證的 `stop_server.ps1`，並在 helper 失敗時顯示可重試錯誤。
- 新增 `http://127.0.0.1:8765/operator` 本機操作頁，整合手機配對、device 管理與
  正式 hardware-lock inspection/recovery。Recovery 仍要求所有軟體檢查、實體
  attestation、精確確認句，並留下 archive、receipt 與 audit；queued shutdown
  會在 recovery 成功後自動繼續。
- 新增針對 fixed-target autonomy、裝置配對／replay／撤銷、active-run shutdown、
  retained-lock gate、dispatch retry 與網頁 recovery 的離線 regression。

## 2026-08-28 (English)

- Normalized target-local 02a retries to the full fixed multiplex target set
  under a full bounded lease; subgroup leases fail before hardware without
  falsely halting the lease.
- Added ten-minute one-time device pairing, independent 12-hour browser sessions,
  and per-device revocation for the public Dashboard.
- Added a full-shutdown control that remains available after autonomy is halted,
  waits for active work, reports dispatch failures, and invokes the verified
  stop helper after the response is delivered.
- Added a loopback-only operator console for device management and formal retained
  hardware-lock recovery with physical attestation, exact confirmation, archive,
  receipt, audit events, and automatic continuation of queued shutdown.

## 2026-08-25

- 統一人機介面為單一 `/session/...` Dashboard：核准、三層控制與所有結果下拉
  紀錄位於同頁；舊 `/approve/...`、`/autonomy/...` 頁面改為相容性 redirect。
- 修正直接 MCP lease request／replacement lease 未重新綁定 session 的問題；所有
  entry path 現在都回傳同一 canonical `browser_url`，避免舊 lease 控制頁與新結果
  分裂。
- 修正 Codex `connection closed: initialize response`：STDIO launcher 改為
  protocol-only，不再於 MCP initialize 前同步依賴 Dashboard、Cloudflare、port
  或網路；網站 bootstrap 改由進入語句觸發的 `Ensure` 處理。
- 修正 root `jy_agent.ps1` 在 `JY_QUALIBRATE_PYTHON` 已設定時的 PowerShell named
  parameter 轉送，並將 Codex startup timeout 提高為 90 秒。
- 新增 root `jy_agent.ps1 -Action Doctor`：以真正 MCP client 執行 initialize／
  tools/list、驗證三種 project config，並將 STDIO、localhost HTTP、Dashboard／
  tunnel 問題分層。新增 [debugQA.md](debugQA.md) 的 Codex、Cursor、Claude 問答式
  runbook。
- STDIO service 會動態讀取後建立的 Dashboard bootstrap metadata；client 不必為
  每次新 tunnel URL 重啟 MCP。
- 完整安全停止會刪除 Dashboard access token 並清空公開 URL metadata，避免曾貼
  入聊天或 log 的 URL 在下一個 service lifetime 重新有效。
- 新增 repository-root `jy_agent.ps1` 入口，使用 `$PSScriptRoot` 與 repo 內部
  相對路徑定位 `JY_agent`，不依賴 `ASqum_QM_lab` 等最外層資料夾名稱。
- 在 repository root 加入 Codex、Claude Code、Cursor 的 MCP config、入口語句
  路由與 Codex skill；使用者不必再把 `JY_agent` 子資料夾開成 workspace。
- `setup.ps1` 現在同時驗證 root 與 nested compatibility config；根目錄
  `jy_agent.ps1 -Action Install/Ensure/Stop/Stdio` 統一委派生命週期操作。
- 更新 README、WORKFLOW 與 CLIENTS 文件，並加入 root discovery／不同 working
  directory 的離線 regression。

## 2026-08-25 (English)

- Unified the human surface on one canonical `/session/...` Dashboard containing
  approvals, all three controls, and the complete experiment dropdown. Legacy
  approval/control pages now redirect there.
- Fixed direct and replacement lease requests so they rebind the stable workflow
  session and return the same canonical `browser_url`.
- Fixed the Codex initialize-response disconnect by making STDIO startup
  protocol-only and moving Dashboard/tunnel startup to entry-time Ensure.
- Fixed named Python forwarding in the root PowerShell shim, increased the Codex
  cold-start timeout, and added a real initialize/tools-list Doctor command.
- Added dynamic Dashboard metadata discovery plus a Q&A runbook covering Codex,
  Cursor, and Claude discovery, trust, reload, HTTP, and tunnel failures.
- Full safe shutdown now removes the Dashboard token and public URL metadata so
  a previously shared link cannot become valid again.
- Added a name-independent repository-root `jy_agent.ps1` shim plus root project
  configs and phrase routing for Codex, Claude Code, and Cursor.
- Opening the repository root is now the recommended workflow; the nested
  `JY_agent` configs remain compatible.
- Extended setup validation, documentation, and offline regressions to cover
  root discovery and launcher resolution outside the current working directory.

## 2026-08-24

- 確認並以 regression 鎖定 `06 → 06b` 的預設順序。
- 所有 reset-capable node 的預設值統一為 thermal；10a、05st 與兩個 06st 不再
  預設 active。
- 新增 active-reset 證據閘門：必須是 active 07b、雙雲團 morphology 通過且
  readout fidelity `>= 0.85`，才可讓該 qubit 的後續實驗使用 active。
- active-reset 統計前必須用 active 複驗並接受 05、06、06b；服務允許這三個
  受限的 off-sequence prerequisite verification，而不改變目前下游節點。
- 依操作者更正移除 05st/06st 的逐筆 100-trace refitting、100/100 否決、重算
  mean/std 與額外分布門檻。保留 `histo_num=100`、同 reset 的基準品質及約四倍
  lifetime 時間窗；前置通過後每個統計節點只跑一次。

## 2026-08-24 (English)

- Locked the default `06 → 06b` ordering with regression coverage.
- Made thermal the default for every reset-capable node and added per-qubit
  active-reset qualification through an active 07b result with compact clouds
  and fidelity of at least 0.85.
- Active-reset statistics now require accepted active-reset verification runs of
  05, 06, and 06b.
- Superseded the 2026-08-23 all-100-refit rule: statistics retain exactly 100
  requested repetitions but run once after validated baselines, with no secondary
  per-iteration refit or all-or-nothing rejection gate.

## 2026-08-23

- Dashboard 新增繁體中文／English 全頁切換，語言偏好保留於瀏覽器。
- Prediction、Parameters、Analysis、Next action 統一改成摺疊面板；Decision 與
  Reason 保持直接顯示。
- 修正副檔名為 `.h5` 的 NetCDF snapshot 載入：依序嘗試 SciPy、h5netcdf 與
  automatic backend。
- 05/06/06b 的完成門檻提高為統計前置品質：relative uncertainty `< 0.25`、
  至少 `3.5` lifetimes；05/06b 另要求 R² `>= 0.90`。
- 05st、06st_t2star、06st_t2e 改為重新 fitting 100 個 raw iterations；必須
  100/100 通過，並核對原始 mean/std、約四倍時間窗與分布穩定性。
- 統計 state candidate 使用 JY 對全部 100 筆的重算結果，不採 percentile-trimmed
  summary；任何統計驗證失敗都抑制 state patch。
- 新增 Cursor project MCP 與 always-on project rule；Codex JY skill 改為可由
  固定進入語句自動觸發。
- `setup.ps1` 可自動尋找 `qualibrate_env` 並驗證三種 client project config。
- policy、playbook、experience 集中到 `rules/`；詳細文件集中到 `docs/`。
- 移除 WireGuard 程式分支與文件，公開手機流程統一為 token-protected Cloudflare
  Dashboard。
- Claude project MCP launcher 改用 `CLAUDE_PROJECT_DIR`，避免啟動目錄不同造成
  相對路徑失效；README 補上可由 coding agent 執行的一次性安裝方式。

## 2026-08-23 (English)

- Added whole-page Traditional Chinese/English selection and persisted browser
  preference.
- Collapsed Prediction, Parameters, Analysis, and Next action while keeping
  Decision and Reason visible.
- Added deterministic NetCDF backend fallback for `.h5` snapshots.
- Strengthened 05/06/06b statistics prerequisites and added all-100-iteration
  validation for 05st/06st nodes.
- Added Cursor project integration, automatic phrase-triggered JY skill routing,
  simpler installation, consolidated rules/docs, and removed WireGuard support.
