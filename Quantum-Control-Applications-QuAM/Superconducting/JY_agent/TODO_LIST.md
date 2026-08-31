# TODO list / 待辦事項

## 尚未完成 / Remaining

### P2 hardening / 後續強化（本輪不實作）

- 實作 system-level 單一控制 Agent ownership lease：同一時間只允許一個已識別
  controller 發出 workflow mutation，包含 owner identity、短效續租、takeover／handoff、
  stale-owner recovery 與 audit；Dashboard 可同時有多個純檢視／人工控制裝置。現有
  SQLite 單一 workflow／lease、idempotency 與 hardware lock 仍保留，但不能把它們
  宣稱為跨 Codex／Claude／Cursor 的完整 controller ownership 保證。
- 若要完全不依賴 Codex `/goal` 或 Claude/Cursor 的 turn 生命週期，新增 server-side
  autonomy scheduler（只消費已核准 lease、沿用相同 policy/analyzer/idempotency、可由
  Dashboard 暫停／停止）；目前 event wait 與 durable goal 已避免正常等待時提早結束，
  但不能把任何第三方 agent host 宣稱為永遠在線的 daemon。
- 收斂 root/client 設定、skill 與 tool contract 的重複內容，加入單一來源產生與
  parity test，避免 `.codex`、`.cursor`、Claude 與 skill 規則日後漂移。
- 盤點並實際 enforcement（或移除）尚未接入 runtime 的 policy keys，例如
  protected paths、always-human、analysis-status pause 等契約，避免設定看似生效。
- 統一 custom MCP/Approval port 在 Ensure、Stop、Doctor、bootstrap 與所有文件的
  傳遞；加入非預設 port 的 lifecycle regression。
- 以正式 ASGI middleware class 取代 Starlette 已 deprecated 的 decorator；補 SSE
  keepalive、慢速／斷線 client 測試，以及 runtime log/event retention 與 rotation。
- Cloudflared 改為固定版本、SHA-256 驗證與 CPU architecture detection；named
  tunnel／固定 hostname 另行評估。
- 拆分偏大的 `service.py`、`mcp_server.py` 與 Dashboard 模組，降低跨功能修改的
  regression 範圍；補 directory fsync／crash-recovery fault-injection 測試。
- [x] 已加入只能由現場 operator 執行的 stale-lock recovery（本機 loopback 網頁，
  互動式 terminal 為 fallback）：
  會驗證 lock/run/request 身分、所有 worker 已死亡、recovery/active/database state
  hash 一致，並在人工確認儀器沒有 active job/output 後封存 lock、留下 receipt 與
  audit events。AI agent 仍不得自行處理 lock。

- 用下一個實際 active-reset 07b 與後續 05/06/06b snapshot 驗證完整的 85% 解鎖、
  conditional prerequisite verification 與統計前置閘門。離線 regression 已完成，
  但本次沒有操作儀器。
- 評估 Cloudflare named tunnel／固定 hostname；目前 quick tunnel 每次重新建立時
  URL 可能改變，但不需手機安裝 VPN App。
- 未來可研究「分析前一筆結果時並行準備下一筆實驗」；在確認 hardware lock、
  state dependency 與取消語意前維持序列執行。
- 未來若需要，可把 AI 對話嵌入 Dashboard；目前 Dashboard 只負責首頁、核准、
  結果與四種 operator 狀態，加上首頁完整關機。
- 觀察一段使用期後，再由維護者決定是否刪除 `approve.ps1`（terminal emergency
  fallback）與 `start_server.ps1`（foreground developer launcher）。目前兩者沒有
  被正常使用流程引用，但仍可能協助離線診斷，因此本輪未猜測性刪除。

## Completed in this update / 本次已完成

- [x] 儀器連線失敗改為可辨識的 pause 狀態；Dashboard 顯示錯誤與恢復步驟，
  no-connect + state-restored case 自動釋放 lock，中途失聯保留 local quarantine。
- [x] 新增跨 client 的 `恢復`／`Recover` close-state recovery，以及完整雙語
  [Command.md](Command.md) 指令表；stale services/workflow 不再要求使用者手動找 PID。
- [x] Dashboard 加入 AI App／外部網路中斷時的獨立 pause、stop、full-shutdown 指引。

- [x] 修正 fixed multiplex `02a` 子群 retry：完整 lease 會正規化為全部 workflow
  targets；窄 lease 在碰硬體前拒絕且不再誤 halt。
- [x] 公開 Dashboard 改為每裝置獨立 session；本機 operator console 可建立 10 分鐘
  一次性手機配對連結、查看與個別撤銷 12 小時 device cookie。
- [x] halted Dashboard 保留獨立的完整結束按鈕；coordinator 先保存 per-session intent、
  等待 active run、將 retained lock 轉為 recovery quarantine、停止 workflow 並呼叫
  正式 `stop_server.ps1`，lock 不再讓網站/MCP/tunnel 持續運作。
- [x] 本機 operator console 加入正式 hardware-lock inspection/recovery；保留實體
  attestation、精確確認句、lock archive、receipt 與 audit；Ensure 在 lock 存在且
  worker 已死亡時只啟動 loopback recovery-only 服務。
- [x] Worker stop 改為 authenticated cooperative request + exit receipt，完整關機
  request 在停止 worker 前寫入 SQLite 與 session-specific JSON；Windows 不再依賴
  `CTRL_BREAK_EVENT`，grace 後僅能 force-stop 經驗證的 PID。
- [x] 自動模式加入 `jy_wait_for_autonomy_status`，Codex skill 使用 session-scoped
  durable goal；Dashboard 同時保留 `已核准` 作為無 durable-goal client 的喚醒方式。

- [x] Repository root 成為唯一 client integration source；移除 `JY_agent` 內層的
  `.codex`、`.cursor`、`.mcp.json`、`.agents/skills` 與 `CLAUDE.md` 相容層，保留
  root 入口及仍被引用的 `JY_agent/AGENTS.md`、`rules/PLAYBOOK.md`。
- [x] P0：公開 Dashboard 不再信任可偽造的 Host header；改用一次性、限時
  bootstrap code 換取 secure cookie，master secret 不再出現在網址或 access log。
- [x] P0：state commit 與 hardware run 互斥、跨 process lock 與唯一 backup；worker
  偵測 active state 非預期變更時 rollback 並保留 hardware lock 供人工檢查。
- [x] P0：Ensure／Stop 使用 lifecycle mutex、atomic bootstrap、instance nonce、
  executable/command/PID 驗證，並清理 public tunnel 與 secrets；run stop 另驗證
  worker process token，避免 PID reuse 誤殺。
- [x] P1：SQLite immediate transaction、唯一索引與 schema migration；所有會改變
  workflow/state/run/autonomy 的 MCP tools 支援可重試 operation idempotency。
- [x] P1：Codex MCP 採 `required=false`；Python 與 Qualibrate config 逐一 probe、
  顯式傳遞並持久化，Doctor 與安裝流程改為 fail-closed 診斷及 constraints 安裝。

- [x] 公開 `/` 成為手機與電腦的共同入口；同一 Session Dashboard 分為首頁、核准、
  結果與控制三頁，舊路徑 redirect，direct/replacement lease 會重新綁定同一 session。
- [x] 結果頁加入 Pause／Resume／End automation（保留網站）／Emergency-stop worker
  （保留網站）；首頁完整關機加入精確二次確認。
- [x] Codex STDIO initialize 不再依賴 Dashboard／Cloudflare，並修正 root Python
  named-parameter forwarding。
- [x] 新增跨 client 的真實 MCP Doctor handshake 與 `debugQA.md` 分層排錯手冊。
- [x] Cursor/Claude 的 project trust、tool catalog 與 reload 問題和 HTTP Ensure
  問題明確拆開。
- [x] Codex、Claude Code、Cursor 可直接從 repository root 以兩個固定入口語句
  啟動 JY；最外層 repo 資料夾改名不影響定位。
- [x] Dashboard 雙語與摺疊面板。
- [x] 06b 固定接在 06 後，並加入順序 regression。
- [x] thermal 預設、active 07b `>= 0.85` 解鎖與 active 05/06/06b 複驗閘門。
- [x] 依更正移除 05st/06st 每一 iteration 的額外 fitting；保留一次 100-repetition run。
- [x] 05/06/06b 至少 3.5 lifetimes 的統計前置門檻與 4.5x 建議時間窗。
- [x] Codex、Claude Code、Cursor project-level MCP bootstrap。
- [x] README／WORKFLOW／UPDATE／TODO 與規則目錄整理。
- [x] WireGuard branch、文件與重複 integration examples 移除。
