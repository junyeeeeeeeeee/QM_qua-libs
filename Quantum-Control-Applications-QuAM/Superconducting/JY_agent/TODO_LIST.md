# TODO list / 待辦事項

## 尚未完成 / Remaining

### 仍未做 / Still open

- Server-side autonomy scheduler（原 P2）。目前 agent 仍依賴 host 的 turn 生命
  週期與 `jy_wait_for_autonomy_status`（單次最多 55 秒）。要完全不依賴任何第三方
  agent host，需要只消費已核准 lease、沿用相同 policy/analyzer/idempotency、可由
  Dashboard 暫停／停止的伺服器端排程器。
- `jy_get_next_action` 目前只內建兩類確定性規則：shared-first-batch，以及
  `analysis.<node>.noise_confirmation_num_averages` 的 03a/04/05 averaging ladder。
  其餘情況回報 `notes` 要求讀規則書。可再把 playbook 已寫成明確數字的梯度接進來：
  02c 的 span 加寬（≤60 MHz）、03a 的 amplitude 0.10→0.075→0.05、07b 的
  readout amplitude ×√0.5、submission timeout 的 multiplex 減半 10→5→3/2→1。
- `workflow_subgroups.resolve_node_targets` 與 analyzer 的 per-target 判定仍是兩套
  實作（見下方 partial-pass 段落）。

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

- [x] 全節點改為首次全目標 multiplex，之後只重測 unresolved 子群（含 02x/02a）；
  誤排已通過目標 soft-reject，不 hard-stop lease
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

## Automatic-mode continuity — closed 2026-09-20

目標：自動模式在量出最終 T1/T2/fidelity 之前，只因儀器斷線無法處理或總時長到期
而停止。

- [x] **崩潰分類處理**。`autonomy.recoverable_worker_exceptions` 是 operator 可編輯
  的簽章表（node + exception type + message marker + 涉及的參數）。命中者保留
  lease 並記錄 `autonomy_recoverable_worker_exception` 與 remedy；未命中的 worker
  例外改為**暫停** lease（`autonomy_paused_for_worker_failure`），以 `恢復量測`
  復原，不再需要重新核准。目前登錄兩條：02c 小數 dBm、03a 在
  `freq_vs_flux_01_quad_term` 為零時的 `arbitrary_qubit_frequency_in_ghz`。
- [x] **這兩條同時做了事前預防**：02c 的 `min_power_dbm`/`max_power_dbm` 政策型別
  改為 `int`；03a 在 quad term 為零時直接拒絕 `arbitrary_qubit_frequency_in_ghz`。
- [x] **碰硬體前的拒絕一律不 halt**。policy violation、scope violation、state patch
  驗證失敗、deterministic setup 驗證失敗、過早 analyze，全部改為記錄
  `autonomy_action_refused` 後拒絕。`halt_conditions` 縮減為 `snapshot_missing`、
  `state_hash_conflict`、`hardware_lock_anomaly`。
- [x] `analyze_authorized_run` 先檢查 run 是否 completed，再跑 watchdog；過早呼叫
  不再因 watchdog 的 hardware-lock 檢查而中止 lease。
- [x] `_is_recoverable_scheduling_block` 改用 exception 型別
  （`AutonomyScopeError`／`AutonomyQuotaError`／`PolicyError`）而非字串比對。
- [x] lease 時長可在 `jy_enter_autonomy_mode(duration_hours=...)` 指定，上限為
  `autonomy.max_duration_hours`（預設 24），核准頁顯示實際時數。

Tests: `tests/test_autonomy_continuity.py`.

## 輕量模型決策支援 — 2026-09-20

- [x] `jy_get_next_action(workflow_id)`：確定性回報 current node、每顆 qubit 的
  resolved/incomplete/unresolved、剩餘 attempts、必要的 deterministic setup 工具，
  以及 `wait`/`analyze`/`record_decision`/`setup`/`run`/`advance`/`blocked`。
  `run.parameters` 只給與節點 defaults 不同的值，並附上套用的規則。沒有登錄規則
  時明說並要求讀規則書，不臆測參數。
- [x] `jy_get_decision_experience` 預設 limit 50→10，且改回傳 `evidence` 摘要
  （每顆 qubit 只留被 gate 的數值）而非完整 analysis 文件；需要時用
  `include_analysis=True`。
- [x] `snr_retry_recommendation` 從 04/05 擴及 03a（三者共用同一組
  `noise_confirmation_num_averages` 梯度）。

Tests: `tests/test_next_action_planner.py`.

## 06 Ramsey would have crashed on its first run — fixed 2026-09-20

`quam_libs/experiments/ramsey/analysis/fetch_dataset.py` called
`convert_IQ_to_V(ds, qubits)` unconditionally, and that helper does `ds["I"]`.
With `use_state_discrimination: true` — JY's configured 06 default — the node
only saves `state{i}` streams, so the first 06 run would have raised
`KeyError: 'I'` with no snapshot and no plot. The repository already contained
`tests/test_ramsey_state_dataset.py` asserting the guard, and that test had been
failing; the guard itself was never in the file's history. The conversion is now
skipped unless both `I` and `Q` are present.

This is a change outside the JY_agent package, in the shared calibration
library. `quam_libs` is not in `protected_paths`, but the node scripts under
`calibration_graph/` were not touched.

## Partial-pass state-commit gap — closed 2026-09-20

- [x] Direction 1 implemented. The analyzer now attributes every failure to the
  qubit that it names, keeps unattributable failures as
  `run_level_failure_reasons` that still block everything, and publishes
  `passing_targets`, `failure_reasons_by_target`, and
  `suppressed_patch_targets`. `candidate_state_patch` is filtered to the
  passing qubits instead of being emptied, and
  `_assert_partial_pass_state_commit` lets an autonomy lease commit a
  `needs_review` run only for those targets (policy key
  `autonomy.partial_pass_state_commit`). A non-passing target is refused
  without halting the lease. Tests: `tests/test_partial_pass_state_commit.py`.

Remaining from the same investigation:

- Direction 2 was not implemented: a target that was resolved *before* this fix
  and never committed still has no way to obtain a pass-backed commit, because
  the node refuses a further run for a resolved target. The operator accepted
  the stored 04 `q1`/`q2` values for that bring-up. Decide later whether to
  allow one extra run for a resolved-but-uncommitted target.
- `workflow_subgroups.resolve_node_targets` and the analyzer's per-target
  verdict are two separate implementations of "did this qubit pass". They agree
  today for the cases we have seen, but a check that exists only in the
  analyzer — the 02x/02a readout-upconverter exclusion, for example — can mark
  a target resolved while `passing_targets` excludes it, which recreates a
  narrow version of this gap. Make one of them the single source of truth.

Original report, for the record:

A run whose overall `analysis_status` is `needs_review` suppresses the state
patch for EVERY qubit in it, including the ones that passed. Combined with the
rule that refuses a further run once all targets are resolved, calibrated
values can end up permanently missing from `state.json` even though the node
reports complete.

Observed three times in workflow `0892399eba47422b96c9203e8ead1523`:

- 02c: nine targets produced passing dressed-plateau evidence across snapshots
  `#135`–`#137`, none committed; recovered only by re-running all ten once the
  gates were relaxed.
- 03a: snapshot `#152` resolved seven targets and committed none; recovered by
  re-running in batches expected to pass together, costing five extra runs.
- 04: `q1` (fitted x180 `0.0367134569` vs stored `0.0364876450`, 0.6 percent)
  and `q2` (`0.020853312` vs `0.020871838`, 0.09 percent) are still
  uncommitted, and no further 04 run is allowed because every target is
  resolved. The operator accepted the stored values for this bring-up and
  asked for the underlying gap to be fixed afterwards.

Directions considered, in `service.py` / the node analyzers:

1. Commit the per-target subset that passed inside a `needs_review` run,
   instead of suppressing the whole patch. — implemented.
2. Allow one more run for a resolved target whose accepted value was never
   committed, so a pass-backed commit can be obtained. — not implemented.

Either way, keep the rule that only passing evidence may be committed.

## Submission timeout is classified as an instrument disconnection — closed 2026-09-20

The operator's splitting rule says a multiplexed run that times out *on
submission* means too many qubits in one program: halve the group, keep
halving, and only treat it as a real disconnection when a single qubit still
times out. That rule cannot currently run autonomously.

`qm.execute` raising `QMTimeoutError` at the 100 s `QuantumMachinesManager`
deadline is classified as `instrument_unreachable`, which pauses the lease and
requires the operator's `恢復量測` phrase before anything can be scheduled
again. So every full-width first attempt at a large node stops the automatic
run and asks a human to inspect hardware that is demonstrably fine — the state
file is unchanged, the hardware lock is correctly not engaged, and the next
half-width run succeeds immediately.

Observed in workflow `5a1839c824e3432c971c925a133fb02a`, run
`b90300ae0a8c451aaa5aaaec267b017d`: 04 Power Rabi, ten qubits, 87 amplitude
points, failed at `qm.execute` with no snapshot; `hardware_lock_recovery_required`
was `false` and `termination_cause` was `instrument_unreachable`.

Distinguishing signal: the timeout happens in `execution_phase`
`hardware_execution` but before any snapshot exists, at the *submission* call,
and the group has more than one target. A genuine disconnection also fails with
a single target, and typically fails at `machine.connect()` rather than at
`add_to_queue`.

Direction: give the classifier a separate `program_submission_timeout` category
for a `QMTimeoutError` raised from `qm.execute`/`add_to_queue` with more than one
target in `parameters.qubits`. That category should not pause the lease; it
should let the agent halve the group and continue, exactly as the B' subgroup
allowance in `_validate_shared_parameter_first_batch` already anticipates. A
single-target submission timeout keeps the current pause-and-hand-off
behaviour.

**Closed.** `failures.classify_failure` now reports
`program_submission_timeout` when a `QMTimeoutError` carries a gRPC
`DEADLINE_EXCEEDED` status, no transport-failure marker, and a `qm` package
frame that submits a program (`execute` / `add_to_queue` / `_add_program` /
`compile`). Because that evidence still cannot separate an oversized program
from a wedged QOP, `instrument_probe.probe_qop` asks the instrument directly
right after the failure — resolve, connect, then ask the QOP to identify
itself — and the worker reclassifies back to `instrument_unreachable` when the
probe does not answer, recording `instrument_probe.cause` as `network` or
`instrument` so the operator message names the right thing to check.

A probe-confirmed submission timeout does not pause: `worker.py` leaves
`measurement_paused` false and `service._enforce_run_hard_stops` records
`autonomy_program_submission_timeout` instead of pausing for review, so the
agent halves the group on its own. A single target that still times out has no
room left to halve, so it pauses with a message naming the QOP job queue rather
than the network. `_node_full_width_submission_failed` accepts both the new and
the old termination cause, keeping the B' subgroup waiver working for runs
recorded either way. Tests:
`tests/test_submission_timeout_classification.py`, plus two cases in
`tests/test_workflow_subgroups.py`.

## A fetch-stage failure always retains the hardware lock — open 2026-09-20

`worker.py` attempts `_stop_active_node_with_evidence()` only for
`instrument_unreachable` and `program_submission_timeout`. Every other
exception raised during `execution_phase: hardware_execution` therefore
retains the hardware lock without JY ever trying to stop the node, which puts
the whole server into recovery-only quarantine and needs an on-site operator at
the loopback console before any measurement can continue.

That is the right default when the instrument is unreachable — there is nothing
to ask. It is too strict when the instrument is provably healthy. Observed in
workflow `2dba0368b00c4b06aeed1a1ceabbb3d9`, run
`450064c6223f4f16bb1f71d6dbb15efc`: the 03a coarse program was submitted and
executed, then `results.fetch_all()` raised
`qm.exceptions.DataFetchingError: UNKNOWN: Unexpected error in RPC handling`.
A reachability probe immediately afterwards completed a cluster health check
against the QOP, so `QualibrationNode.active_node.stop()` would very likely
have succeeded and released the lock on verified evidence.

Direction: attempt the node stop for any failure in the hardware-execution
phase, not just the two recognized categories, and let the existing
`hardware_cleanup_verified` evidence decide whether the lock releases. Keep
retaining the lock whenever that stop cannot be confirmed. This does not weaken
the rule that an unverified process is never killed and a retained lock is never
deleted; it only stops JY from skipping the one check that could produce the
evidence.

Related: the operator decided 2026-09-20 that a repeated `DataFetchingError` at
the fetch stage should be treated like an oversized program and handled by the
halving ladder. Registering it in `autonomy.recoverable_worker_exceptions` would
also keep the lease alive so the agent can halve without an operator.
