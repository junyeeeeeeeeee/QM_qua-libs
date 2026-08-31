# Bounded autonomy / 有限自動量測

`進入 JY 自動量測模式` 會建立一份人工核准的 bounded lease。預設有效 8 小時、
整批 run 數不設上限、每個 `(node, qubit)` 最多 20 次。某個 qubit 達上限時只把
它標成該 node 未完成；其他已有證據的 qubit 繼續往後。

`進入 JY 自動量測模式` creates a human-approved bounded lease. It lasts eight
hours, has no batch-wide run cap, and allows at most 20 attempts for each
`(node, qubit)`. Exhausting one target marks only that target incomplete; other
resolved targets continue.

Codex 支援 durable goal 時，這個進入語句會建立一個 session-scoped goal；Terra
與 Sol 使用相同生命週期，並以 `jy_wait_for_autonomy_status` 等待核准、worker 或
控制事件。模型不是常駐 daemon，終止由 goal 完成、operator stop、lease expiry、
hard stop 或 host 終止 turn 決定。Claude/Cursor 沒有跨平台的強制常駐 API；若 host
暫停，回原對話輸入 `已核准` 即可從 server-side SQLite/lease 狀態續跑。

When Codex durable goals are available, this phrase creates a session-scoped
goal; Terra and Sol share the same lifecycle and wait through
`jy_wait_for_autonomy_status`. A model is not a resident daemon: completion,
operator stop, lease expiry, hard stop, or host turn termination ends execution.
Claude/Cursor have no universal persistence API; `已核准` resumes from the
server-side SQLite/lease state if their host pauses.

只有 snapshot missing、worker failure、state hash conflict、hardware lock anomaly
或 parameter policy violation 會立即停止整個排程。低信心、`needs_review`、
`manual_review` 不會自動暫停，但不能授權 unattended state commit。

Only snapshot loss, worker failure, state-hash conflict, a hardware-lock anomaly,
or a parameter-policy violation halts the entire schedule immediately. Low
confidence, `needs_review`, and `manual_review` do not pause the lease, but they
cannot authorize an unattended state commit.

Dashboard 的結果頁提供四種 authorization 狀態：本次完成後暫停、繼續、結束授權
但保留網站、緊急停止 worker 但保留網站。首頁另有需精確二次確認的「完整結束
量測」按鈕，即使 lease 已 `halted` 仍可使用。完整結束會
先保存 session-specific shutdown request，以 authenticated stop file 要求 worker
協作停止，等待 terminal、永久停止 workflow，再關閉 MCP、Dashboard 與 tunnel。
若 retained hardware lock 仍存在，確認所有 worker 已死亡後會轉為 quarantine；
lock 不刪除，但服務照常關閉。下次 Ensure 僅在 loopback 啟動 recovery-only 頁面。

The Results page exposes four authorization states: pause after the current run,
resume, end/revoke automation while keeping the site, and emergency-stop the
worker while keeping the site. A separate confirmation-gated full-shutdown
button remains on Home after a lease is halted. It first persists a session
shutdown request, delivers an authenticated cooperative stop, waits for a
terminal run, stops the workflow, then closes MCP, Dashboard, and tunnel. If a
lock remains after every worker is verified dead, the lock is quarantined (not
deleted) while services still close. The next Ensure starts loopback
recovery-only service for the on-site operator.

`02x` 與 `02a` 是 fixed multiplex nodes。在完整 targets 的 bounded lease 中，
即使 Decision 只指出某一個 qubit 需要 retry，server 也會把該次 run 的 `qubits`
正規化為完整 workflow targets；只涵蓋子群的 lease 會在接觸硬體前被拒絕，而且
不會把既有 lease 誤標成 halted。

`02x` and `02a` are fixed multiplex nodes. Under a full-target bounded lease,
a target-local retry decision is normalized to the complete workflow target set.
A subgroup-only lease is rejected before hardware access without falsely halting
an otherwise valid lease.

完整限制以 [rules/policies.yaml](../rules/policies.yaml) 為準，科學流程以
[rules/PLAYBOOK.md](../rules/PLAYBOOK.md) 為準。

The enforced limits live in [rules/policies.yaml](../rules/policies.yaml); the
scientific workflow lives in [rules/PLAYBOOK.md](../rules/PLAYBOOK.md).
