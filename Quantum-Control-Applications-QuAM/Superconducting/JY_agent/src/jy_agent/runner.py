from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .util import (
    atomic_write_json,
    exclusive_file_lock,
    json_dumps,
    json_loads,
    utc_now,
)


class RunnerError(RuntimeError):
    pass


class ExperimentRunner:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.db = database

    def start_approved_run(self, proposal_id: str, actor: str) -> dict[str, Any]:
        proposal = self.db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if proposal is None:
            raise RunnerError(f"Unknown proposal: {proposal_id}")
        if proposal["kind"] != "run":
            raise RunnerError("Only a run proposal can start an experiment")
        if proposal["status"] != "approved":
            raise RunnerError("Run proposal has not been approved in the human CLI")
        if datetime.fromisoformat(proposal["expires_at"]) <= datetime.now(timezone.utc):
            self.db.execute(
                "UPDATE proposals SET status = 'expired' WHERE id = ?", (proposal_id,)
            )
            raise RunnerError("Run approval has expired")
        if proposal["uses"] >= proposal["max_uses"]:
            raise RunnerError("Run approval has no remaining uses")

        payload = json_loads(proposal["payload_json"], {})
        run_id = uuid.uuid4().hex
        process_token = uuid.uuid4().hex
        self._acquire_lock(run_id)
        request_path = self.settings.runtime / "requests" / f"{run_id}.json"
        log_path = self.settings.runtime / "logs" / f"{run_id}.log"
        state_updates_path = request_path.with_name(f"{run_id}.state_updates.json")
        recovery_path = self.settings.runtime / "recovery" / f"{run_id}-state.json"
        stop_request_path = request_path.with_name(f"{run_id}.stop.json")
        exit_receipt_path = request_path.with_name(f"{run_id}.exit.json")
        request = {
            "run_id": run_id,
            "workflow_id": proposal["workflow_id"],
            "proposal_id": proposal_id,
            "node_id": payload["node_id"],
            "node_name": payload["node_name"],
            "script_path": payload["script_path"],
            "parameters": payload["parameters"],
            "data_root": str(self.settings.data_root),
            "active_state": str(self.settings.active_state),
            "wiring_path": str(self.settings.wiring_path),
            "qualibrate_config_path": str(self.settings.qualibrate_config_path),
            "database_path": str(self.settings.database_path),
            "lock_path": str(self.settings.lock_path),
            "state_updates_path": str(state_updates_path),
            "recovery_state_path": str(recovery_path),
            "stop_request_path": str(stop_request_path),
            "exit_receipt_path": str(exit_receipt_path),
            "process_token": process_token,
        }
        run_recorded = False
        try:
            atomic_write_json(request_path, request)
            with self.db.connect() as connection:
                changed = connection.execute(
                    """
                    UPDATE proposals SET uses = uses + 1
                    WHERE id = ? AND status = 'approved' AND uses < max_uses
                    """,
                    (proposal_id,),
                ).rowcount
                if changed != 1:
                    raise RunnerError("Approval was consumed concurrently")
                connection.execute(
                    """
                    INSERT INTO runs(
                        id, workflow_id, proposal_id, node_id, parameters_json,
                        status, started_at, log_path, autonomy_lease_id,
                        process_token, exit_receipt_path
                    ) VALUES (?, ?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        proposal["workflow_id"],
                        proposal_id,
                        payload["node_id"],
                        json_dumps(payload["parameters"]),
                        utc_now(),
                        str(log_path),
                        proposal.get("autonomy_lease_id"),
                        process_token,
                        str(exit_receipt_path),
                    ),
                )
            run_recorded = True

            command = [
                str(self.settings.qualibrate_python),
                "-m",
                "jy_agent.worker",
                "--request",
                str(request_path),
                "--process-token",
                process_token,
            ]
            environment = os.environ.copy()
            python_paths = [
                str(self.settings.agent_root / "src"),
                str(self.settings.superconducting_root),
            ]
            if environment.get("PYTHONPATH"):
                python_paths.append(environment["PYTHONPATH"])
            environment["PYTHONPATH"] = os.pathsep.join(python_paths)
            environment["QUALIBRATE_CONFIG_FILE"] = str(
                self.settings.qualibrate_config_path
            )
            creation_flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            with log_path.open("ab") as log_handle:
                process = subprocess.Popen(
                    command,
                    cwd=self.settings.superconducting_root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                )
        except Exception as exc:
            self._release_lock(run_id)
            if run_recorded:
                self.db.execute(
                    """
                    UPDATE runs
                    SET status = 'failed', error = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (str(exc), utc_now(), run_id),
                )
            raise
        self.db.execute(
            "UPDATE runs SET status = 'running', pid = ? WHERE id = ?",
            (process.pid, run_id),
        )
        self.db.event(
            "run_started",
            actor,
            {"run_id": run_id, "node_id": payload["node_id"], "pid": process.pid},
            proposal["workflow_id"],
        )
        self._monitor_worker_exit(
            process,
            run_id=run_id,
            workflow_id=proposal["workflow_id"],
            exit_receipt_path=exit_receipt_path,
        )
        return {
            "run_id": run_id,
            "status": "running",
            "pid": process.pid,
            "log_path": str(log_path),
            "approval_uses": {
                "used": proposal["uses"] + 1,
                "maximum": proposal["max_uses"],
            },
        }

    def stop(
        self,
        run_id: str,
        actor: str,
        *,
        intent: str = "operator_stop",
        reason: str | None = None,
    ) -> dict[str, Any]:
        run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if run is None:
            raise RunnerError(f"Unknown run: {run_id}")
        if run["status"] not in {"starting", "running", "stopping"}:
            return {"run_id": run_id, "status": run["status"], "stopped": False}
        pid = run["pid"]
        if not isinstance(pid, int):
            raise RunnerError("Run has no process id yet")
        self._verify_worker_process(run)
        requested_at = utc_now()
        request_path = self.settings.runtime / "requests" / f"{run_id}.stop.json"
        atomic_write_json(
            request_path,
            {
                "run_id": run_id,
                "process_token": run["process_token"],
                "pid": pid,
                "intent": intent,
                "reason": reason,
                "actor": actor,
                "requested_at": requested_at,
            },
        )
        self.db.execute(
            "UPDATE runs SET status = 'stopping', stop_intent = ?, "
            "stop_requested_at = ? WHERE id = ?",
            (intent, requested_at, run_id),
        )
        self.db.event(
            "run_stop_requested",
            actor,
            {"run_id": run_id, "intent": intent, "reason": reason},
            run["workflow_id"],
        )
        return {
            "run_id": run_id,
            "status": "stopping",
            "stopped": True,
            "intent": intent,
            "delivery": "cooperative_stop_request",
        }

    def force_stop(self, run_id: str, actor: str) -> dict[str, Any]:
        """Terminate only the recorded worker; retain the lock for lab inspection."""
        run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if run is None:
            raise RunnerError(f"Unknown run: {run_id}")
        if run["status"] not in {"starting", "running", "stopping"}:
            return {"run_id": run_id, "status": run["status"], "forced": False}
        pid = run.get("pid")
        if not isinstance(pid, int):
            raise RunnerError("Run has no process id yet")
        self._verify_worker_process(run)
        try:
            os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
        except ProcessLookupError:
            pass
        message = (
            "Emergency stop grace period elapsed; the exact worker was terminated. "
            "The hardware lock was retained for manual safety inspection."
        )
        self.db.execute(
            "UPDATE runs SET status = 'force_stopped', finished_at = ?, error = ?, "
            "termination_cause = 'forced_after_grace' "
            "WHERE id = ?",
            (utc_now(), message, run_id),
        )
        self.db.event(
            "run_force_stopped",
            actor,
            {"run_id": run_id, "pid": pid, "hardware_lock_retained": True},
            run["workflow_id"],
        )
        return {
            "run_id": run_id,
            "status": "force_stopped",
            "forced": True,
            "hardware_lock_retained": True,
        }

    def exit_evidence(self, run_id: str) -> dict[str, Any]:
        """Return authenticated stop/exit evidence without exposing worker secrets."""
        evidence: dict[str, Any] = {}
        for label, suffix in (("stop_request", ".stop.json"), ("exit_receipt", ".exit.json")):
            path = self.settings.runtime / "requests" / f"{run_id}{suffix}"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue
            if isinstance(payload, dict) and payload.get("run_id") == run_id:
                payload.pop("process_token", None)
                evidence[label] = payload
        return evidence

    def _monitor_worker_exit(
        self,
        process: subprocess.Popen[bytes],
        *,
        run_id: str,
        workflow_id: str,
        exit_receipt_path: Path,
    ) -> None:
        """Keep the Popen handle so Windows exit codes remain observable."""

        def supervise() -> None:
            exit_code = process.wait()
            try:
                receipt = json.loads(exit_receipt_path.read_text(encoding="utf-8"))
                if not isinstance(receipt, dict) or receipt.get("run_id") != run_id:
                    receipt = {"run_id": run_id}
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                receipt = {"run_id": run_id}
            receipt.pop("process_token", None)
            receipt["os_exit_code"] = exit_code
            receipt["observed_at"] = utc_now()
            atomic_write_json(exit_receipt_path, receipt)
            termination_cause = str(
                receipt.get("termination_cause")
                or ("normal_exit" if exit_code == 0 else "process_exit")
            )
            self.db.execute(
                "UPDATE runs SET exit_code = ?, exit_receipt_path = ?, "
                "termination_cause = COALESCE(termination_cause, ?) WHERE id = ?",
                (exit_code, str(exit_receipt_path), termination_cause, run_id),
            )
            self.db.event(
                "worker_process_exited",
                "runner-supervisor",
                {
                    "run_id": run_id,
                    "exit_code": exit_code,
                    "termination_cause": termination_cause,
                },
                workflow_id,
            )

        threading.Thread(
            target=supervise,
            name=f"jy-worker-exit-{run_id[:8]}",
            daemon=True,
        ).start()

    @staticmethod
    def process_is_alive(pid: int) -> bool:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            try:
                if not ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                ):
                    return False
                return exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _acquire_lock(self, run_id: str) -> None:
        self.settings.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with exclusive_file_lock(
                self.settings.runtime / "hardware-transition.lock",
                "hardware run start",
            ):
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                try:
                    descriptor = os.open(self.settings.lock_path, flags)
                except FileExistsError as exc:
                    try:
                        owner = json.loads(
                            self.settings.lock_path.read_text(encoding="utf-8")
                        )
                    except Exception:
                        owner = {"raw": "unreadable"}
                    raise RunnerError(
                        f"Hardware is locked by another run: {owner}"
                    ) from exc
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump({"run_id": run_id, "created_at": utc_now()}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
        except RuntimeError as exc:
            raise RunnerError(str(exc)) from exc

    def _verify_worker_process(self, run: dict[str, Any]) -> None:
        pid = run.get("pid")
        token = str(run.get("process_token") or "")
        if not isinstance(pid, int) or not token:
            raise RunnerError("Worker process identity is incomplete; refusing to signal it")
        command_line = self._process_command_line(pid)
        request_path = self.settings.runtime / "requests" / f"{run['id']}.json"
        required = ("jy_agent.worker", str(request_path), token)
        command_folded = command_line.casefold()
        if not command_line or any(
            value.casefold() not in command_folded for value in required
        ):
            raise RunnerError(
                "Recorded PID no longer matches the exact JY worker command; "
                "refusing to signal a possibly unrelated process"
            )

    @staticmethod
    def _process_command_line(pid: int) -> str:
        if os.name == "nt":
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return completed.stdout.strip() if completed.returncode == 0 else ""
        try:
            return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(
                b"\x00", b" "
            ).decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _release_lock(self, run_id: str) -> None:
        try:
            payload = json.loads(self.settings.lock_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            return
        if payload.get("run_id") == run_id:
            self.settings.lock_path.unlink(missing_ok=True)


def release_worker_lock(lock_path: Path, run_id: str) -> None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if payload.get("run_id") == run_id:
        lock_path.unlink(missing_ok=True)
