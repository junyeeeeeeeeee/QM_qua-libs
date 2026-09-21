# Repository agent instructions

Treat ordinary requests as normal repository work and do not invoke JY tools.

Only when the user says the entry phrase `進入 JY 量測模式` or
`進入 JY 自動量測模式` (accept `進入JY量測模式`,
`進入JY自動量測模式`, `Enter JY measurement mode`, and
`Enter JY automatic measurement mode` as exact aliases), read and follow
`Quantum-Control-Applications-QuAM/Superconducting/JY_agent/AGENTS.md`,
`Quantum-Control-Applications-QuAM/Superconducting/JY_agent/rules/PLAYBOOK.md`,
and the server-enforced policies. The repository-root MCP configuration launches
`jy_bringup` through `jy_agent.ps1`; this shim locates `JY_agent` relative to the
repository root, so the outer repository folder name is irrelevant. The
host-local `.codex/config.toml` must nevertheless keep an absolute `-File` path
to the repository-root shim and an absolute `cwd`. Codex mobile/remote resume can
start the MCP child outside the repository, so do not replace those values with
`.\\jy_agent.ps1` and `cwd = "."`. If a checkout moves, update both values to the
new absolute repository root before reloading Codex.

On either exact entry phrase, the project-owned STDIO MCP must already be
attached. Run `powershell -NoProfile -ExecutionPolicy Bypass -File
.\jy_agent.ps1 -Action Ensure` to start or reuse the HTTP services and Dashboard,
then use the requested JY mode and return the shared Dashboard URL. If
Ensure reports `recovery_only`, do not call an entry tool or create a public
Dashboard; report the loopback operator console for on-site recovery. If
the client uses an offline or network-restricted sandbox, request normal host
user and network permission for `Ensure` before running it. Public Dashboard
bootstrap must not run as a sandbox-only Windows identity because its private
token ACL would prevent the host-owned services from reading it; never weaken or
bypass the ACL check as a fallback. If
`jy_bringup` is absent, do not treat Ensure as an MCP attachment mechanism: run
`.\jy_agent.ps1 -Action Doctor`, enable/trust the project MCP, and reload or
create a new client session. Do not claim entry succeeded until the corresponding
`jy_enter_*` tool returns successfully.
If Ensure or entry fails because a prior JY process, workflow, or lifecycle
record was left open, tell the operator to issue `恢復` or `Recover`. In JY
context, run `powershell -NoProfile -ExecutionPolicy Bypass -File
.\jy_agent.ps1 -Action Recover`, report its structured result, and retry entry
only when it reports `closed`. It must never delete a retained hardware lock or
terminate an unverified process. `closed_recovery_required` means all services
are closed but the on-site safety quarantine still requires the loopback
operator console.
For automatic mode, use `jy_wait_for_autonomy_status` rather than ending the
task while approval or a run result is pending. If the host provides durable
goals, the automatic-mode entry phrase authorizes one session-scoped goal that
continues until workflow completion, operator stop, lease expiry, or a hard
stop. Conversational mode never creates that goal. A client without durable
goals cannot promise indefinite agent lifetime and must use the Dashboard's
`已核准` wake-up fallback if its host pauses the turn.
Return only the entry result's top-level `browser_url`; approval, control, and
result-history fields are internal navigation metadata for the same Dashboard
service, not separate websites. The public `browser_url` is the common `/` entry;
its Home, Approval, and Experiment results pages share one session and origin.
Return the bare Cloudflare URL without token/query parameters. Each browser signs
in with the fixed password stored in ignored
`JY_agent/runtime/dashboard-password.txt`; do not create or request device-pairing links.
After the operator sends `已核准` / `Approved`, the next reply starts with
exactly `已核准`. While measurement mode is open, any paused turn copies
`operator_handoff.chat` as a three-item markdown list, never as one paragraph,
so the operator always sees what to do, which exact phrase to send afterwards,
and the shutdown hint. A live turn may execute
`結束量測` directly; a paused or disconnected turn needs Dashboard Home
shutdown first, then `結束量測` to verify.

Treat configured stop/exit phrases including `退出 JY 量測模式`, `結束量測`, and
`停止量測` as a full safe shutdown: persist the shutdown request, cooperatively
stop or poll any active run, leave or quarantine the JY workflow, then run
`powershell -NoProfile -ExecutionPolicy Bypass -File .\jy_agent.ps1 -Action
Stop`. A retained hardware lock is preserved for local recovery but, once no
worker is alive, must not keep the Dashboard, MCP service, or public tunnel
running. Never approve on the user's behalf, execute the
calibration files directly, write `state.json` directly, remove the hardware
lock, or expose the MCP listener publicly.

Recognized instrument-connectivity failures pause the current experiment and
new scheduling instead of presenting a generic crash. Do not schedule another
run until the operator fixes QOP/OPX, instrument power, or the lab network and
issues `恢復量測` / `Resume measurement`; call
`jy_resume_measurement_mode` for the same workflow. The current node restarts
from the beginning as a new run; never claim that the interrupted Python stack
continues. A retained hardware lock still blocks resume and requires the local
operator recovery console. If the AI client disconnects, Dashboard controls remain authoritative;
after connectivity returns, `結束量測` or `End measurement` verifies full
shutdown. Treat `暫停 JY 自動量測` / `Pause JY automatic measurement`, `繼續 JY
自動量測` / `Resume JY automatic measurement`, `結束 JY 自動授權` / `End JY
automatic authorization`, and `緊急停止 JY worker` / `Emergency stop JY worker`
as the corresponding bounded controls. See
`Quantum-Control-Applications-QuAM/Superconducting/JY_agent/Command.md` for the
complete bilingual command contract.
