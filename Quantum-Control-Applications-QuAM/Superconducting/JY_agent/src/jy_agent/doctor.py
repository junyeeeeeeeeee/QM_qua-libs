from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

_LOOPBACK_OPENER = build_opener(ProxyHandler({}))


def _same_absolute_path(value: Any, expected: Path) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        candidate = Path(value).expanduser()
        return candidate.is_absolute() and candidate.resolve() == expected.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _health(
    url: str,
    expected_service: str,
    *,
    health_token: str = "",
    expected_nonce: str = "",
) -> dict[str, Any]:
    try:
        # Local service diagnosis must never be routed through a corporate or
        # user HTTP proxy; proxy timeouts otherwise look like dead JY services.
        request = Request(url)
        if health_token:
            request.add_header("X-JY-Health-Token", health_token)
        with _LOOPBACK_OPENER.open(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        service_ok = payload.get("service") == expected_service
        nonce_ok = (
            not expected_nonce
            or payload.get("instance_nonce") == expected_nonce
        )
        return {
            "ok": service_ok and nonce_ok,
            "service": payload.get("service"),
            "pid": payload.get("pid"),
            "instance_nonce_matches": nonce_ok,
        }
    except Exception as exc:  # Diagnostic output, not a control path.
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _project_configs(repository_root: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    json_paths = {
        "cursor": repository_root / ".cursor" / "mcp.json",
        "claude": repository_root / ".mcp.json",
    }
    for name, path in json_paths.items():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            server = payload["mcpServers"]["jy_bringup"]
            arguments = [str(value) for value in server.get("args", [])]
            joined = " ".join(arguments)
            command_ok = Path(str(server.get("command", ""))).name.casefold() in {
                "powershell.exe",
                "powershell",
                "pwsh.exe",
                "pwsh",
            }
            launcher_ok = "jy_agent.ps1" in joined
            stdio_ok = "Stdio" in arguments
            checks[name] = {
                "ok": command_ok and launcher_ok and stdio_ok,
                "path": str(path),
            }
        except Exception as exc:
            checks[name] = {
                "ok": False,
                "path": str(path),
                "error": f"{type(exc).__name__}: {exc}",
            }
    codex_path = repository_root / ".codex" / "config.toml"
    try:
        payload = tomllib.loads(codex_path.read_text(encoding="utf-8"))
        server = payload["mcp_servers"]["jy_bringup"]
        arguments = [str(value) for value in server.get("args", [])]
        expected_launcher = repository_root / "jy_agent.ps1"
        file_index = next(
            (
                index
                for index, value in enumerate(arguments)
                if value.casefold() == "-file"
            ),
            -1,
        )
        launcher = (
            arguments[file_index + 1]
            if 0 <= file_index < len(arguments) - 1
            else None
        )
        launcher_ok = _same_absolute_path(launcher, expected_launcher)
        cwd_ok = _same_absolute_path(server.get("cwd"), repository_root)
        required = server.get("required")
        optional_ok = required is False
        command_ok = Path(str(server.get("command", ""))).name.casefold() in {
            "powershell.exe",
            "powershell",
            "pwsh.exe",
            "pwsh",
        }
        stdio_ok = "Stdio" in arguments
        problems: list[str] = []
        if not command_ok:
            problems.append("Codex jy_bringup must use PowerShell.")
        if not launcher_ok:
            problems.append(
                "Codex -File must use the absolute repository-root jy_agent.ps1 path."
            )
        if not cwd_ok:
            problems.append(
                "Codex cwd must be the absolute repository root; relative cwd breaks "
                "mobile/remote thread resume."
            )
        if not optional_ok:
            problems.append(
                "Codex jy_bringup must use required=false so an optional JY "
                "startup failure cannot block ordinary project startup/resume."
            )
        if not stdio_ok:
            problems.append("Codex jy_bringup must use the Stdio action.")
        checks["codex"] = {
            "ok": command_ok and launcher_ok and cwd_ok and optional_ok and stdio_ok,
            "required": required,
            "startup_timeout_sec": server.get("startup_timeout_sec"),
            "path": str(codex_path),
            "problems": problems,
        }
    except Exception as exc:
        checks["codex"] = {
            "ok": False,
            "path": str(codex_path),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return checks


def _bootstrap_summary(agent_root: Path) -> dict[str, Any]:
    path = agent_root / "runtime" / "server-bootstrap.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {
            "ok": False,
            "path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": True,
        "path": str(path),
        "status": payload.get("status") or "ready",
        "remote_provider": payload.get("remote_provider"),
        "public_url_configured": bool(payload.get("public_base_url")),
        "mcp_pid": payload.get("mcp_pid"),
        "approval_pid": payload.get("approval_pid"),
        "public_tunnel_pid": payload.get("public_tunnel_pid"),
        "updated_at": payload.get("updated_at"),
    }


async def _stdio_probe(
    repository_root: Path, timeout_seconds: float
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ModuleNotFoundError as exc:
        return {
            "ok": False,
            "error": f"Missing MCP dependency: {exc}",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    parameters = StdioServerParameters(
        command="powershell.exe",
        args=[
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repository_root / "jy_agent.ps1"),
            "-Action",
            "Stdio",
        ],
        cwd=repository_root,
        env={
            **os.environ,
            "JY_QUALIBRATE_PYTHON": os.environ.get(
                "JY_QUALIBRATE_PYTHON", sys.executable
            ),
        },
    )

    async def initialize() -> dict[str, Any]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                result = await session.initialize()
                listing = await session.list_tools()
                names = {tool.name for tool in listing.tools}
                required = {
                    "jy_get_status",
                    "jy_enter_measurement_mode",
                    "jy_enter_autonomy_mode",
                }
                return {
                    "ok": required.issubset(names),
                    "server_name": result.server_info.name,
                    "server_version": result.server_info.version,
                    "tool_count": len(names),
                    "required_tools_present": sorted(required & names),
                    "missing_required_tools": sorted(required - names),
                }

    try:
        outcome = await asyncio.wait_for(initialize(), timeout=timeout_seconds)
    except Exception as exc:
        outcome = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    outcome["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return outcome


async def run_doctor(
    repository_root: Path, timeout_seconds: float = 90.0
) -> dict[str, Any]:
    repository_root = repository_root.expanduser().resolve()
    agent_root = (
        repository_root
        / "Quantum-Control-Applications-QuAM"
        / "Superconducting"
        / "JY_agent"
    )
    configs = _project_configs(repository_root)
    bootstrap = _bootstrap_summary(agent_root)
    stdio = await _stdio_probe(repository_root, timeout_seconds)
    bootstrap_payload: dict[str, Any] = {}
    if bootstrap.get("ok"):
        try:
            bootstrap_payload = json.loads(
                (agent_root / "runtime" / "server-bootstrap.json").read_text(
                    encoding="utf-8-sig"
                )
            )
        except Exception:
            bootstrap_payload = {}
    mcp_port = 8765
    approval_port = int(bootstrap_payload.get("approval_port") or 8766)
    try:
        from urllib.parse import urlsplit

        endpoint = urlsplit(str(bootstrap_payload.get("mcp_endpoint") or ""))
        if endpoint.hostname in {"127.0.0.1", "localhost", "::1"} and endpoint.port:
            mcp_port = endpoint.port
    except (TypeError, ValueError):
        pass
    health_token = ""
    token_path = bootstrap_payload.get("approval_access_token_path")
    if isinstance(token_path, str) and token_path:
        try:
            health_token = Path(token_path).read_text(encoding="ascii").strip()
        except OSError:
            health_token = ""
    expected_nonce = str(bootstrap_payload.get("instance_nonce") or "")
    local_services = {
        "mcp_http": _health(
            f"http://127.0.0.1:{mcp_port}/healthz",
            "jy-mcp",
            expected_nonce=expected_nonce,
        ),
        "approval_http": _health(
            f"http://127.0.0.1:{approval_port}/healthz",
            "jy-approval",
            health_token=health_token,
            expected_nonce=expected_nonce,
        ),
    }
    infrastructure_ok = all(value["ok"] for value in local_services.values())
    suggestions: list[str] = []
    if not stdio["ok"]:
        suggestions.append(
            "STDIO handshake failed: inspect runtime/mcp-stdio-launch.log, then "
            "run the root Doctor action again before debugging Dashboard/network."
        )
    elif not all(value["ok"] for value in configs.values()):
        suggestions.append(
            "STDIO works but a project config is invalid: repair that client's "
            "root config and reload/trust the workspace."
        )
    else:
        suggestions.append(
            "STDIO and project configs pass. If a client still lacks jy_bringup, "
            "enable/trust the project MCP and create a new or reloaded session."
        )
    if not infrastructure_ok:
        suggestions.append(
            "Dashboard service is not ready; run jy_agent.ps1 -Action Ensure "
            "after MCP is attached and before entering a measurement mode."
        )
    if (
        bootstrap.get("ok")
        and bootstrap.get("status") == "ready"
        and not infrastructure_ok
    ):
        suggestions.append(
            "Bootstrap metadata says ready while loopback health is down; treat "
            "it as stale metadata. Ensure will verify processes and replace it; "
            "if entry still fails, issue '恢復' or 'Recover' to close verified "
            "stale JY state before retrying."
        )
    return {
        "ok": bool(stdio["ok"] and all(v["ok"] for v in configs.values())),
        "measurement_started": False,
        "repository_root": str(repository_root),
        "stdio": stdio,
        "project_configs": configs,
        "local_services": local_services,
        "infrastructure_ok": infrastructure_ok,
        "bootstrap": bootstrap,
        "suggestions": suggestions,
        "security_note": (
            "Diagnostic output never includes the Dashboard access token."
        ),
    }
