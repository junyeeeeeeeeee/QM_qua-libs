# JY_agent

JY_agent 是綁定此 Qualibrate repository 的安全量測代理層。它讓 Codex、
Claude Code 或 Cursor Agent 透過相同的 MCP 工具、政策、Dashboard 與稽核紀錄
執行量測；AI 不會直接執行 calibration script、寫入 `state.json`，也不會代替
人員核准。

JY_agent is the policy-gated measurement-agent layer for this Qualibrate
repository. Codex, Claude Code, and Cursor Agent share the same MCP tools,
Dashboard, policies, and audit records. The AI never runs calibration scripts
directly, writes `state.json` directly, or approves on behalf of an operator.

## 安裝 / Installation

前提是 Qualibrate 與本 repository 已可正常使用。第一次只需在 repository
最上層執行一行：

```powershell
powershell -ExecutionPolicy Bypass -File .\jy_agent.ps1 -Action Install
```

`setup.ps1` 會優先自動找到 `qualibrate_env`，以 editable mode 安裝 JY_agent，
初始化資料庫並檢查 Codex、Claude、Cursor 的 project config。自動搜尋失敗時才需：

```powershell
powershell -ExecutionPolicy Bypass -File .\jy_agent.ps1 -Action Install -Python C:\path\to\qualibrate_env\python.exe
```

也可以直接在已開啟 repository 最上層的 coding agent 對話輸入：

```text
請執行 .\jy_agent.ps1 -Action Install 完成 JY_agent 的一次性離線安裝與設定驗證，不要開始量測。
```

Agent 只需使用一般 shell 能力完成這次安裝；此時不依賴 JY MCP，也不會接觸儀器。

Assuming Qualibrate and this repository already work, run the first command
once from the repository root. It discovers `qualibrate_env`, installs the package in
editable mode, initializes storage, and validates all three client configs. Use
`-Python` only when automatic discovery cannot find the environment. You may
instead ask the coding agent to run `jy_agent.ps1 -Action Install` for one-time offline installation
and configuration validation. This step needs ordinary shell access, not the JY
MCP, and does not operate instruments.

## 什麼是「載入 JY_agent」？ / What does “load JY_agent” mean?

它現在是指用 AI coding client 把整個 repository 最上層開成 workspace/project；
不必再把 `JY_agent` 子資料夾另外開啟：

- Codex 讀取根目錄由 Install 依 `.codex/config.toml.example` 產生的 host-local
  `.codex/config.toml`、`AGENTS.md` 與 `.agents/skills/`。
- Claude Code 讀取根目錄 `.mcp.json` 與 `CLAUDE.md`。
- Cursor 讀取根目錄 `.cursor/mcp.json` 與 `.cursor/rules/jy-agent.mdc`。

第一次開啟時，各 client 可能顯示一次 project MCP trust 提示；這是唯一必要的
client 安全確認。接受後，client 會在背景啟動根目錄 `jy_agent.ps1`，再由它定位
並啟動 `JY_agent/start_mcp_stdio.ps1`；這一層只建立 STDIO MCP tools，不啟動
網站或 tunnel。收到進入語句後，agent 才在背景執行 `Ensure` 建立網站服務，
使用者不需開著終端機。定位只依 repository 內部相對路徑，因此
`ASqum_QM_lab` 或其他最外層資料夾名稱都可以。

It now means opening the repository root as the AI client's workspace or
project; opening the nested `JY_agent` folder is no longer required. Each
supported client discovers its root project configuration. Codex's active
`.codex/config.toml` is host-local and generated from the checked-in example.
The first open may show a one-time MCP trust prompt. After that, the client
launches the root `jy_agent.ps1`, which resolves and starts the nested
`start_mcp_stdio.ps1`. This phase creates only the STDIO MCP tools; the agent runs
Ensure after an entry phrase to establish the website/tunnel. No user-operated
terminal is needed, and the outer repository folder can have any name.

純網頁版 chatgpt.com 本身不能啟動本機 MCP。ChatGPT/Codex 的本機桌面工作階段
可以；手機 remote 是回到這台量測電腦上既有的對話，而不是讓手機直接連儀器。

The browser-only chatgpt.com product cannot launch a local project MCP by
itself. A local Codex/ChatGPT desktop agent can. Mobile remote control continues
the desktop conversation; the phone never connects directly to instruments.

## 執行量測 / Run an agentic experiment

在已載入專案的對話輸入以下其中一句：

```text
進入 JY 量測模式
```

不必再附加 `target:` 或 `multiplex:`。新 workflow 的 qubit 清單來自
`state.json` 的 `active_qubit_names`，`multiplexed` 預設為 true。若要量不同
qubit，請先改 `active_qubit_names`，不要在對話裡重寫一份 target 清單。

空格可省略為 `進入JY量測模式`；自動模式同樣可輸入
`進入JY自動量測模式`。英文指令分別是 `Enter JY measurement mode` 與
`Enter JY automatic measurement mode`；完整中英文指令表請見
[Command.md](Command.md)。

一般模式一次討論、核准並執行一個實驗，每次 run/state change 都需在同一個
Dashboard 頁面核准。

```text
進入 JY 自動量測模式
```

自動模式只先核准一次 8 小時的有限授權。其後只要 targets、nodes、參數與
20 次／node／qubit 上限均在授權內，就會繼續安全量測；硬停損仍立即停止。
Codex 支援 durable `/goal` 時，進入自動模式會建立一個只屬於該 session 的持續
目標，並以 `jy_wait_for_autonomy_status` 等待核准或新結果，不會只因暫時沒有工具
可呼叫就結束。Terra 與 Sol 服從相同 goal/turn 控制；模型本身不決定無限執行，
真正的終止條件是 workflow 完成、使用者停止、lease 到期、硬停損，或 host 結束
該 turn。Claude/Cursor 沒有由本 repo 強制保持無限 turn 的通用 API，因此會盡量
維持當前 agent run；若 host 暫停，Dashboard 會提示回對話輸入 `已核准` 續跑。

Enter either exact phrase in a project-loaded conversation. Do not append
`target:` or `multiplex:`; a new workflow uses `state.json` `active_qubit_names`
and defaults `multiplexed=true`. Conversational mode
requires approval for every run or state change. Automatic mode requests one
bounded authorization -- eight hours by default, specifiable at entry -- and then continues only inside its targets,
nodes, safe parameters, and 20-attempt-per-node/qubit limit.
The exact English entry commands are `Enter JY measurement mode` and
`Enter JY automatic measurement mode`; see [Command.md](Command.md) for all
bilingual controls.
When Codex durable goals are available, automatic entry creates one
session-scoped goal and waits through `jy_wait_for_autonomy_status`; Terra and
Sol follow the same goal/turn lifecycle. Completion, operator stop, lease expiry,
and hard stops are terminal. Repository instructions cannot guarantee an
indefinite Claude/Cursor turn, so `已核准` remains the host-resume fallback.

預設校正順序為 `02x → 02c → 02a`，並包含 `06 → 06b`。所有 reset 預設為 thermal；只有 active-reset
07b 得到緊實雙雲團且 fidelity 至少 85% 的 qubit，後續才可選 active。若統計節點
採 active，會先以 active 複驗 05、06、06b；之後 05st 與 06st 各執行一次，不會
再逐條 fitting 100 個 iteration。

The default calibration sequence is `02x → 02c → 02a`, and includes `06 → 06b`. Reset defaults to thermal.
Downstream active reset is allowed per qubit only after an active-reset 07b result
has compact dual clouds and at least 85% fidelity. Active-reset statistics first
revalidate 05, 06, and 06b under active reset; each statistics node then runs once
without a second all-iteration refit gate.

`start_mcp_stdio.ps1` 必須先讓 client 擁有 MCP 工具，這是「雞生蛋」邊界：完全
沒有 MCP 的 agent 無法只靠一句自然語言建立 MCP。專案載入負責這個一次性的
protocol bootstrap；它不依賴 Dashboard、Cloudflare、port 或網路。輸入進入語句
後，agent 才會執行 `ensure_server.ps1` 並建立或恢復量測 workflow，絕不會因為
開啟資料夾就自動操作硬體。這個兩階段設計也確保 tunnel 失敗不會讓 MCP 在
`initialize` response 前斷線。

The stdio MCP must exist before an agent can interpret a phrase as an MCP
action—this is the bootstrap boundary. Project loading supplies that lightweight
protocol bootstrap without depending on the Dashboard, Cloudflare, ports, or
network. The entry phrase then runs Ensure and creates or resumes the measurement
workflow. Merely opening the folder never operates hardware, and a tunnel failure
cannot break the MCP initialize response.

## Dashboard

進入語句觸發的 Ensure 只把 MCP HTTP service 綁在 `127.0.0.1:8765`，並把
review-only Dashboard 透過 Cloudflare HTTPS quick tunnel 公開。Cloudflare 裸 URL
不帶任何 token；所有電腦與手機直接開啟同一網址，輸入
`runtime/dashboard-password.txt` 的固定密碼後取得 secure cookie。此密碼首次
正常啟動時產生一次並固定沿用，不會 commit 或寫入網址。公開頁面不能呼叫 MCP
或任意執行程式。

每次進入模式只對使用者顯示 entry result 最外層的 `browser_url`；這是手機與電腦
共用的 `/` 首頁入口。首頁會解析目前 session，並提供三個分頁：首頁（狀態與
量測控制與完整關機）、核准、實驗結果。舊 `/approve/...`、`/autonomy/...` 與
`/session/...` 連結只會導向對應的新分頁，不會建立另一個網站。直接建立或替換
lease 也會重新綁定同一個 workflow session。

結果頁的下拉選單保留本次 server 生命週期內所有實驗，且只在新結果、判斷或控制
狀態出現時更新；核准頁只在提案改變時更新。右上角可選繁體中文或 English；時間
預測、參數、分析與下一步預設為摺疊面板。結果頁提供暫停、繼續、結束自動授權
（保留網站）與緊急停止 worker（保留網站）；只有首頁提供完整關機，且需輸入
`SHUTDOWN <session-id>` 防止誤觸。

手機或第二個瀏覽器直接使用同一裸 URL 與固定密碼，不再需要配對連結；每個瀏覽器
仍取得各自 12 小時 cookie。本機 `http://127.0.0.1:8765/operator` 只提供
retained hardware lock 的正式檢查／recovery，仍要求現場實體確認與完整確認句，
且不會經 public tunnel。本機 Dashboard 是 `http://127.0.0.1:8766/`；Cloudflare
quick-tunnel hostname 在建立新 tunnel 後可能改變，以當次 entry 回傳的網址為準。

The MCP remains loopback-only at `127.0.0.1:8765`. Only the review Dashboard is
forwarded through a password-protected Cloudflare HTTPS quick tunnel. The bare
URL contains no secret; every browser enters the fixed ignored runtime password
and receives an independent 12-hour secure cookie. The public surface cannot
invoke MCP or arbitrary code. Agents show only the entry result's
top-level `browser_url`, a common `/` entry for phones and computers. It resolves
the active session and links to Home, Approval, and Experiment results. Legacy
approval, autonomy, and session URLs redirect to the appropriate view. The
Results selector retains every run for that server session; updates are
event-driven. Language is selectable at the top right, and Prediction,
Parameters, Analysis, and Next action are collapsed by default. Results provides
Pause, Resume, End automation (keep site), and Emergency-stop worker (keep site).
Only Home provides full shutdown, guarded by the exact
`SHUTDOWN <session-id>` confirmation. Direct or replacement lease requests also
rebind the workflow's stable session to the current lease.
Phones and secondary browsers use the same URL and password; no pairing link is
required. The loopback-only `/operator` page provides formal retained-lock
inspection/recovery with physical attestation and is never tunneled.

離開或停止語句（例如 `結束量測`、`停止量測`、`退出 JY 量測模式`）會先安全
終止／等待 active run，再關閉 workflow、Dashboard、MCP HTTP service 與 tunnel。
完整停止也會刪除該次 Dashboard token；下一次進入會產生全新連結。
Dashboard 的「完整結束量測」按鈕在 lease halted 後仍可使用；它會等待 active run，
若 worker 無法證明 cleanup 而留下 lock，coordinator 會確認所有 JY worker 都已死亡，
保留 lock 與 recovery marker，但仍關閉 Dashboard、MCP 與 tunnel。下一次 Ensure
只會啟動 loopback recovery-only 服務，不會重新公開網站；現場 operator 復原 lock
後，才能再次進入量測模式。Worker 會在接觸硬體前先持久化 pre-run state hash，
避免 abrupt exit 讓正式 recovery 缺少必要證據；既有舊 lock 只在 active/configured/
protected-backup state 完全相符時使用相容復原。不得手動刪除 lock。

Exit or stop phrases safely stop or poll an active run, close the workflow, and
then shut down the Dashboard, HTTP MCP service, and public tunnel. Full shutdown
also deletes that Dashboard token, so the next entry receives a fresh URL.
The full-shutdown button remains available after a halted lease and waits for an
active run. If cleanup leaves a lock, JY verifies that every worker has exited,
keeps the lock/receipt for recovery, and still closes Dashboard, MCP, and tunnel.
The next Ensure is loopback recovery-only until an on-site operator formally
recovers the lock. Before hardware access, workers persist a pre-run state hash
so an abrupt exit cannot make formal recovery evidence impossible. Legacy locks
use the compatibility path only when active, configured, and protected-backup
state hashes are identical. Never delete a lock manually.

若 QOP/OPX 或儀器無法連線，worker 會暫停本次實驗與後續排程，Dashboard 直接顯示
檢查與恢復方式。可證明連線未建立，或硬體執行中仍能找到 active node、成功 stop，
且 state 已回復時，會自動釋放本次 lock 並保留網站；現場確認後輸入
`恢復量測`，同一 workflow/session 會恢復，目前 node 以新 run-id 從頭執行。
若 worker 被強制關閉、active node 無法證明停止或 state 回復失敗，lock 留在本機
safety quarantine；完整關機仍會關閉網站、MCP 與 tunnel。

AI App 或網路斷線時，Dashboard 的暫停、停止與完整關機仍可獨立使用。網路恢復後
可輸入 `結束量測`／`End measurement` 確認收尾。若下次進入因殘留 process、workflow
或 lifecycle 設定失敗，輸入 `恢復`／`Recover`；agent 會執行 root
`jy_agent.ps1 -Action Recover`，不依賴 JY MCP，也不會強制刪除 hardware lock。

If QOP/OPX or an instrument cannot be reached, JY classifies the outage and
pauses the run and new scheduling; the Dashboard shows the recovery steps. A
provable no-connect failure releases its lock after state restoration. A
mid-execution disconnect also releases only when the recorded active node stops
successfully and state restoration verifies; otherwise it remains in local
quarantine. After inspection, `Resume measurement` restores the same session and
restarts the current node as a new run. Full shutdown still closes the site, MCP
service, and tunnel. If the AI app or network
disconnects, Dashboard controls remain usable. After reconnection, use `End
measurement`; if a later entry is blocked by stale state, use `Recover`.

## 主要檔案架構 / Main file structure

```text
repository root/
├─ jy_agent.ps1                       root install/lifecycle/MCP shim
├─ .mcp.json / .codex/ / .cursor/     root client discovery
├─ AGENTS.md / CLAUDE.md / .agents/   root phrase routing
└─ Quantum-Control-Applications-QuAM/Superconducting/JY_agent/
   ├─ README.md / Command.md / WORKFLOW.md / UPDATE.md / TODO_LIST.md
   ├─ AGENTS.md                           detailed agent instructions
   ├─ setup.ps1                          installation implementation
   ├─ start_mcp_stdio.ps1                client-owned MCP launcher
   ├─ diagnose_mcp.ps1                   non-measurement MCP self-test
   ├─ ensure_server.ps1 / stop_server.ps1 server and tunnel lifecycle
   ├─ recover_closed_state.ps1           safe stale-state close coordinator
   ├─ config/agent.yaml                  non-policy service configuration
   ├─ rules/
   │  ├─ PLAYBOOK.md                     scientific decision rules
   │  ├─ policies.yaml                   server-enforced hard limits
   │  └─ experiences/                    accepted settings and lessons
   ├─ docs/                              detailed operating documentation
   ├─ src/jy_agent/                      MCP, policy, worker, Dashboard
   ├─ tests/                             offline regression tests
   └─ runtime/                           ignored local audit/runtime data
```

關機／停止的可稽核資料位於 `runtime/`：SQLite 的 `runs` 與
`shutdown_requests` 保存狀態；`requests/<run-id>.stop.json` 是帶 process token
驗證的協作停止請求；`requests/<run-id>.exit.json` 是 worker/OS 退出證據；
`shutdown_requests/<session-id>.json` 讓不同 Dashboard session 不會互相覆蓋；
`recovery-required.json` 與 `hardware.lock` 則保留現場復原邊界。這些都是 ignored
local runtime data，不應 commit、分享 token，或手動修改。

Shutdown/stop evidence lives under `runtime/`: SQLite records run and shutdown
state; per-run stop/exit JSON files authenticate delivery and record process exit;
per-session shutdown JSON prevents stale-session collisions; the recovery marker
and hardware lock preserve the on-site recovery boundary. These local files are
ignored, secret-bearing runtime state and must not be edited or committed.

詳細連結 / Detailed links:

- [WORKFLOW.md](WORKFLOW.md): 從 0 開始的程式與指令流程 / from-zero program flow.
- [Command.md](Command.md): 中英文自然語言與維護指令 / bilingual conversation and maintenance commands.
- [rules/PLAYBOOK.md](rules/PLAYBOOK.md): 科學判讀與節點規則 / scientific node rules.
- [rules/policies.yaml](rules/policies.yaml): 硬性安全限制 / enforced safety limits.
- [docs/CLIENTS.md](docs/CLIENTS.md): Codex、Claude、Cursor 設定 / client setup.
- [debugQA.md](debugQA.md): MCP、Dashboard 與 client 問答式排錯 / Q&A troubleshooting.
- [docs/PUBLIC_DASHBOARD.md](docs/PUBLIC_DASHBOARD.md): 公開 Dashboard 安全模型 / public Dashboard security.
- [docs/AUTONOMY.md](docs/AUTONOMY.md): 有限自動量測 / bounded autonomy.
- [UPDATE.md](UPDATE.md) and [TODO_LIST.md](TODO_LIST.md): 更新與待辦 / changes and remaining work.
