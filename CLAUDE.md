# Repository-root JY routing

For normal repository work, do not use JY tools. On the phrases
`進入 JY 量測模式` or `進入 JY 自動量測模式`, including the aliases
`進入JY量測模式`, `進入JY自動量測模式`, `Enter JY measurement mode`, and
`Enter JY automatic measurement mode`, read and follow
`Quantum-Control-Applications-QuAM/Superconducting/JY_agent/AGENTS.md` and
`Quantum-Control-Applications-QuAM/Superconducting/JY_agent/rules/PLAYBOOK.md`.
Use the project-scoped `jy_bringup` MCP server discovered from this root
`.mcp.json`; `jy_agent.ps1` resolves the nested JY package without depending on
the outer repository folder name.

The project launcher initializes only the client-owned STDIO protocol. On an
entry phrase, first run `.\jy_agent.ps1 -Action Ensure` to start or reuse the
loopback HTTP services and public Dashboard; then call the requested entry tool.
If Ensure returns `recovery_only`, do not call an entry tool or publish a URL;
direct the on-site operator to the loopback recovery console.
Conversational mode requires approval for every experiment or state change.
Automatic mode requests one bounded lease and then uses only `jy_autonomy_*`
tools within its authorized scope. Wait for approval/results with
`jy_wait_for_autonomy_status` instead of ending the agent run merely because no
action is ready. Project instructions cannot guarantee an indefinite Claude
turn; if the host pauses, resume the same session after the operator sends
`已核准`. Return only the entry result's top-level
`browser_url`; approval and control URL fields are aliases for that same shared
Dashboard service, not additional websites. The common `/` entry links to the
session Home, Approval, and Results & controls pages.
Never approve on the operator's behalf or bypass JY policies.

For configured stop/exit phrases, persist full shutdown, cooperatively stop or
poll the active run, leave or quarantine the workflow, then run `powershell
-NoProfile -ExecutionPolicy Bypass -File .\jy_agent.ps1 -Action Stop`. A
retained lock is preserved for local recovery but does not keep public or local
JY services alive after the worker exits. If MCP is unavailable, report that measurement
mode did not start, run `.\jy_agent.ps1 -Action Doctor`, and ask the user to
trust/enable and reload this repository-root MCP configuration; do not ask them
to open the nested `JY_agent` folder and do not retry Ensure as an attachment fix.

In JY context, `恢復` / `Recover` means run repository-root
`.\jy_agent.ps1 -Action Recover` to safely converge stale workflow/process state
and close verified services. Never delete a retained lock or kill an unverified
process. Instrument-connectivity failures pause scheduling and are shown on the
Dashboard. The complete bilingual aliases are in
`Quantum-Control-Applications-QuAM/Superconducting/JY_agent/Command.md`.
