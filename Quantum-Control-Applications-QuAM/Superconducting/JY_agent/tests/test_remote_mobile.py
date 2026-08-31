from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from jy_agent.doctor import _project_configs


AGENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = AGENT_ROOT.parents[2]


class RemoteMobileTests(unittest.TestCase):
    def test_idempotent_bootstrap_uses_token_protected_public_tunnel(self) -> None:
        script = (AGENT_ROOT / "ensure_server.ps1").read_text(encoding="utf-8")
        self.assertIn('"http://127.0.0.1:$ApprovalPort"', script)
        self.assertIn('"http://127.0.0.1:$McpPort/healthz"', script)
        self.assertIn("cloudflared", script.casefold())
        self.assertIn("trycloudflare", script)
        self.assertIn("-WindowStyle Hidden", script)
        self.assertIn("JY_APPROVAL_ACCESS_TOKEN", script)
        self.assertIn("public-dashboard-access.token", script)
        self.assertIn("public-dashboard-bootstrap.json", script)
        self.assertIn("bootstrap_code=", script)
        self.assertIn("JY_SERVICE_INSTANCE_NONCE", script)
        self.assertIn("JYAgentLifecycle", script)
        self.assertIn("Refusing to stop an unidentified listener", script)
        self.assertIn("JY_RECOVERY_ONLY", script)
        self.assertIn("Get-LiveJyWorkers", script)
        self.assertIn("recovery_only = $RecoveryOnly", script)
        self.assertIn('operator_console = "http://127.0.0.1:$McpPort/operator"', script)
        self.assertIn(
            'Local is a temporary recovery quarantine, not a persistent user',
            script,
        )
        self.assertIn('$RemoteProvider = "public"', script)
        self.assertIn('[bool]$Saved.recovery_only -ne $RecoveryOnly', script)
        self.assertNotIn('"0.0.0.0"', script)

    def test_full_shutdown_rotates_public_dashboard_token(self) -> None:
        script = (AGENT_ROOT / "stop_server.ps1").read_text(encoding="utf-8")
        self.assertIn('Join-Path $Runtime "public-dashboard-access.token"', script)
        self.assertIn("Remove-Item -LiteralPath $SecretPath -Force", script)
        self.assertIn("approval_access_token_path", script)
        self.assertIn("dashboard_access_token_removed", script)
        self.assertIn("instance_nonce", script)
        self.assertIn("Write-AtomicJson", script)
        self.assertIn("recovery-required.json", script)
        self.assertIn("Get-LiveJyWorkers", script)
        self.assertIn("hardware_lock_retained", script)
        self.assertNotIn(
            'throw "The JY hardware lock still exists; refusing to shut down services."',
            script,
        )

    def test_legacy_remote_launchers_and_docs_were_removed(self) -> None:
        self.assertFalse((AGENT_ROOT / "start_remote_server.ps1").exists())
        self.assertFalse((AGENT_ROOT / "REMOTE_APPROVAL.zh-TW.md").exists())
        self.assertTrue((AGENT_ROOT / "docs" / "PUBLIC_DASHBOARD.md").is_file())

    def test_nested_workspace_integration_is_removed(self) -> None:
        launcher = (AGENT_ROOT / "start_mcp_stdio.ps1").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("& $EnsureScript", launcher)
        self.assertIn("STDIO initialization must never depend", launcher)
        self.assertIn("-m jy_agent stdio", launcher)
        self.assertIn("Resolve-JyEnvironment", launcher)
        for redundant_path in (
            AGENT_ROOT / ".codex",
            AGENT_ROOT / ".cursor",
            AGENT_ROOT / ".agents",
            AGENT_ROOT / ".mcp.json",
            AGENT_ROOT / "CLAUDE.md",
        ):
            self.assertFalse(redundant_path.exists(), redundant_path)
        self.assertTrue((AGENT_ROOT / "AGENTS.md").is_file())
        self.assertTrue((AGENT_ROOT / "rules" / "PLAYBOOK.md").is_file())

    def test_repository_root_clients_forward_to_jy_agent(self) -> None:
        launcher_path = REPOSITORY_ROOT / "jy_agent.ps1"
        launcher = launcher_path.read_text(encoding="utf-8")
        self.assertIn("$PSScriptRoot", launcher)
        self.assertIn('"Quantum-Control-Applications-QuAM"', launcher)
        self.assertIn('"JY_agent"', launcher)
        self.assertIn('"Resolve"', launcher)
        self.assertNotIn("ASqum_QM_lab", launcher)
        self.assertIn('"Doctor"', launcher)
        self.assertIn('"Recover"', launcher)
        self.assertIn('"recover_closed_state.ps1"', launcher)
        self.assertIn("QualibrateConfig", launcher)

        claude_path = REPOSITORY_ROOT / ".mcp.json"
        cursor_path = REPOSITORY_ROOT / ".cursor" / "mcp.json"
        for path in (claude_path, cursor_path):
            configuration = json.loads(path.read_text(encoding="utf-8"))
            server = configuration["mcpServers"]["jy_bringup"]
            self.assertIn("jy_agent.ps1", " ".join(server["args"]))
            self.assertIn("Stdio", server["args"])
        self.assertIn(
            "${CLAUDE_PROJECT_DIR:-.}",
            claude_path.read_text(encoding="utf-8"),
        )

        codex = (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn("[mcp_servers.jy_bringup]", codex)
        self.assertIn("jy_agent.ps1", codex)
        self.assertIn('"Stdio"', codex)

        cursor_rule = (
            REPOSITORY_ROOT / ".cursor" / "rules" / "jy-agent.mdc"
        ).read_text(encoding="utf-8")
        root_agents = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        root_skill = (
            REPOSITORY_ROOT
            / ".agents"
            / "skills"
            / "jy-qubit-bringup"
            / "SKILL.md"
        ).read_text(encoding="utf-8")
        for instructions in (cursor_rule, root_agents, root_skill):
            self.assertIn("進入 JY 量測模式", instructions)
            self.assertIn("進入 JY 自動量測模式", instructions)
            self.assertIn("進入JY量測模式", instructions)
            self.assertIn("進入JY自動量測模式", instructions)
            self.assertIn("Enter JY measurement mode", instructions)
            self.assertIn("Enter JY automatic measurement mode", instructions)
            self.assertIn("Recover", instructions)

        commands = (AGENT_ROOT / "Command.md").read_text(encoding="utf-8")
        self.assertIn("進入 JY 量測模式", commands)
        self.assertIn("Enter JY measurement mode", commands)
        self.assertIn("恢復", commands)
        self.assertIn("Recover", commands)

    def test_root_launcher_resolves_from_a_different_working_directory(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if powershell is None:
            self.skipTest("PowerShell is unavailable on this platform")
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(REPOSITORY_ROOT / "jy_agent.ps1"),
                "-Action",
                "Resolve",
            ],
            cwd=AGENT_ROOT / "tests",
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(Path(completed.stdout.strip()).resolve(), AGENT_ROOT)

    def test_root_launcher_forwards_named_python_parameter_safely(self) -> None:
        launcher = (REPOSITORY_ROOT / "jy_agent.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "& $TargetScript -Python $Python -QualibrateConfig $QualibrateConfig",
            launcher,
        )
        self.assertNotIn("@ForwardedArguments", launcher)

    def test_doctor_rejects_relative_codex_paths_for_remote_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory).resolve()
            codex_directory = repository_root / ".codex"
            codex_directory.mkdir()
            (repository_root / "jy_agent.ps1").touch()
            config_path = codex_directory / "config.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[mcp_servers.jy_bringup]",
                        'command = "powershell.exe"',
                        'args = ["-File", ".\\\\jy_agent.ps1", "-Action", "Stdio"]',
                        'cwd = "."',
                        "required = false",
                        "startup_timeout_sec = 90",
                    ]
                ),
                encoding="utf-8",
            )

            relative_check = _project_configs(repository_root)["codex"]
            self.assertFalse(relative_check["ok"])
            self.assertEqual(len(relative_check["problems"]), 2)

            launcher = str(repository_root / "jy_agent.ps1").replace(
                "\\", "\\\\"
            )
            cwd = str(repository_root).replace("\\", "\\\\")
            config_path.write_text(
                "\n".join(
                    [
                        "[mcp_servers.jy_bringup]",
                        'command = "powershell.exe"',
                        f'args = ["-File", "{launcher}", "-Action", "Stdio"]',
                        f'cwd = "{cwd}"',
                        "required = false",
                        "startup_timeout_sec = 90",
                    ]
                ),
                encoding="utf-8",
            )

            absolute_check = _project_configs(repository_root)["codex"]
            self.assertTrue(absolute_check["ok"])
            self.assertEqual(absolute_check["problems"], [])

    def test_debug_qa_and_doctor_are_shipped(self) -> None:
        self.assertTrue((AGENT_ROOT / "diagnose_mcp.ps1").is_file())
        debug_qa = (AGENT_ROOT / "debugQA.md").read_text(encoding="utf-8")
        self.assertIn("jy_agent.ps1 -Action Doctor", debug_qa)
        self.assertIn("Ensure", debug_qa)
        self.assertIn("Cursor", debug_qa)
        self.assertIn("Claude", debug_qa)


if __name__ == "__main__":
    unittest.main()
