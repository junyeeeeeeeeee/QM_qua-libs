---
name: jy-qubit-bringup
description: Operate JY superconducting-qubit measurement from the repository-root workspace when the user uses a Chinese or English JY measurement-mode entry phrase. Use the policy-gated JY MCP for conversational or bounded automatic calibration, approvals, snapshots, decisions, and safe shutdown.
---

# JY repository-root entry

Read the complete nested instructions before taking JY actions:

- `../../../Quantum-Control-Applications-QuAM/Superconducting/JY_agent/AGENTS.md`
- `../../../Quantum-Control-Applications-QuAM/Superconducting/JY_agent/rules/PLAYBOOK.md`
- `../../../Quantum-Control-Applications-QuAM/Superconducting/JY_agent/rules/policies.yaml`

The project MCP launches through `../../../jy_agent.ps1 -Action Stdio`. Its path
is relative to the repository root, so never depend on the outer folder name.
STDIO startup is protocol-only. On either entry phrase, first run repository-root
`jy_agent.ps1 -Action Ensure` to establish the loopback services and Dashboard;
then call the selected entry tool. If Ensure returns `recovery_only`, do not call
an entry tool or expose a public URL; direct the on-site operator to the returned
loopback operator console and wait for formal lock recovery.

For `進入 JY 量測模式`, call `jy_enter_measurement_mode`, then propose exactly
one registered run or state change at a time and wait for each Dashboard approval.
For `進入 JY 自動量測模式`, call `jy_enter_autonomy_mode`, wait for the bounded
lease approval with `jy_wait_for_autonomy_status`, then use only `jy_autonomy_*`
execution/state tools within scope. This exact automatic-mode phrase also
authorizes a host that supports durable goals (for example Codex `/goal`) to
create one goal for this measurement session. Its objective is to keep
advancing the authorized JY workflow until one of these terminal conditions:
the workflow completes, the operator stops it, the lease expires, or a
server-enforced hard stop occurs. Do not create a durable goal for conversational
mode. While the automatic goal is active, use the bounded wait tool whenever no
action is ready instead of ending the task merely because approval or a worker
result is pending. Mark the goal complete only at a real terminal condition.
If the client has no durable-goal facility, keep the current turn alive while it
can, state that host continuity is not guaranteed, and rely on the Dashboard's
`已核准` wake-up instruction if the host pauses the conversation.
`進入JY量測模式` and `進入JY自動量測模式` select the same respective modes.
`Enter JY measurement mode` and `Enter JY automatic measurement mode` are the
exact English aliases.
For either mode, show only the entry result's top-level `browser_url`; approval
and control fields are internal navigation metadata for the same Dashboard and
must not be presented as extra websites. The public `/` entry links to Home,
Approval, and Results & controls pages for the active session.
Never approve for the operator or bypass policy, snapshot, state-hash, lock, or
quota gates.

For configured stop/exit wording, request full shutdown and poll any active run.
The worker first receives a cooperative authenticated stop request; after the
grace period only its verified PID may be force-stopped. Then leave/quarantine
the workflow and run `powershell -NoProfile -ExecutionPolicy Bypass -File
.\jy_agent.ps1 -Action Stop`. A retained lock is never deleted: once no JY
worker is alive, it becomes a local recovery quarantine and does not keep the
Dashboard, MCP service, or public tunnel running. If `jy_bringup` is unavailable, do not claim entry
succeeded or retry Ensure as an attachment fix; run `jy_agent.ps1 -Action Doctor`,
then enable/trust and reload the repository-root project MCP.

If entry fails because stale JY process/workflow/lifecycle state remains, route
the JY-context command `恢復` / `Recover` to repository-root
`jy_agent.ps1 -Action Recover`. This path does not need the MCP, never removes a
retained hardware lock, and never kills an unverified process. Retry entry only
after a `closed` result; `closed_recovery_required` means services are closed but
local safety recovery is still required.

Treat recognized instrument-connectivity failures as paused measurement state,
not a generic crash. Do not schedule another experiment until connectivity is
repaired and the operator resumes. If the AI app disconnects, Dashboard controls
remain available; after reconnection, `結束量測` / `End measurement` verifies
full shutdown. Read
`../../../Quantum-Control-Applications-QuAM/Superconducting/JY_agent/Command.md`
when a bilingual control alias is needed.
