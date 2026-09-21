from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Collection
from urllib.parse import urlsplit

from .analysis import AnalysisError, SnapshotAnalyzer, evidence_digest
from .config import Settings
from .db import Database
from .policy import PolicyEngine, PolicyError
from .runner import ExperimentRunner, RunnerError
from .state import (
    bootstrap_patch,
    commit_state,
    drive_lo_recenter_patch,
    filter_patch,
    json_diff,
    load_state,
    operation_amplitude,
    patch_qubit_targets,
    snapshot_state_path,
    StateError,
)
from .util import (
    atomic_write_json,
    exclusive_file_lock,
    json_dumps,
    json_loads,
    parse_iso_datetime,
    sha256_file,
    utc_now,
)
from .workflow_02c import resolve_02c_targets
from .workflow_subgroups import (
    SHARED_FIRST_BATCH_NODES,
    SUBGROUP_NODES,
    resolve_03a_candidate_targets,
    resolve_03a_shift_ready_targets,
    resolve_node_targets,
)


class ServiceError(RuntimeError):
    pass


class AutonomyScopeError(ServiceError):
    pass


class AutonomyQuotaError(ServiceError):
    """A per-target attempt budget is exhausted without invalidating the lease."""


class AutonomyEvidenceError(ServiceError):
    pass


SHUTDOWN_HINT = "如需結束量測，請於網頁首頁結束量測後再對話輸入「結束量測」。"


def operator_handoff(do: str, reply: str) -> dict[str, Any]:
    """Fixed chat footer: what the operator must do, then which phrase to send."""
    lines = [
        f"需要使用者做什麼: {do}",
        f"完成後回傳: {reply}",
        SHUTDOWN_HINT,
    ]
    return {
        "do": do,
        "reply": reply,
        "shutdown_hint": SHUTDOWN_HINT,
        "lines": lines,
        "chat": "\n".join(f"- {line}" for line in lines),
    }


def approval_operator_handoff() -> dict[str, str]:
    return operator_handoff(
        "開啟對話中的 Dashboard 網址並登入，到 Approval 頁核准。",
        "已核准",
    )


def instrument_operator_handoff() -> dict[str, str]:
    return operator_handoff(
        "現場檢查 QOP/OPX、儀器電源與實驗室網路，確認儀器可連線。",
        "恢復量測",
    )


def submission_escalation_operator_handoff() -> dict[str, str]:
    """Handoff for a single-target program the QOP still would not accept."""

    return operator_handoff(
        "單一 qubit 的程式仍送不進 QOP，且儀器有回應健康檢查，"
        "請檢查 QOP 工作佇列或重啟 QOP。",
        "恢復量測",
    )


def recover_operator_handoff() -> dict[str, str]:
    return operator_handoff(
        "讓系統安全收尾殘留的 workflow、process 與 JY 服務。",
        "恢復",
    )


def recovery_console_operator_handoff() -> dict[str, str]:
    return operator_handoff(
        "到量測電腦本機 loopback operator console 完成硬體鎖現場確認。",
        "恢復",
    )


def host_pause_operator_handoff() -> dict[str, str]:
    return operator_handoff(
        "無需新操作。對話若已被平台暫停，回原對話喚醒即可。",
        "已核准",
    )


def resume_autonomy_operator_handoff() -> dict[str, str]:
    return operator_handoff(
        "確認可繼續後，恢復同一份未過期的自動授權。",
        "繼續 JY 自動量測",
    )


class AgentService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self.settings.ensure_runtime()
        self.db = Database(self.settings.database_path)
        self.policy = PolicyEngine(self.settings)
        self.runner = ExperimentRunner(self.settings, self.db)
        self.analyzer = SnapshotAnalyzer(self.settings, self.policy)
        self._approval_secret = self._load_or_create_approval_secret()
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()

    def idempotent_call(
        self,
        operation: str,
        operation_id: str | None,
        request: dict[str, Any],
        callback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Replay one completed mutation and reject conflicting/concurrent reuse."""
        if operation_id is None:
            return callback()
        key = operation_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", key):
            raise ServiceError(
                "operation_id must be 8-128 stable URL-safe characters"
            )
        request_hash = hashlib.sha256(
            json_dumps(request).encode("utf-8")
        ).hexdigest()
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM idempotency_records "
                "WHERE operation = ? AND operation_id = ?",
                (operation, key),
            ).fetchone()
            if row is not None:
                record = dict(row)
                if not hmac.compare_digest(record["request_sha256"], request_hash):
                    raise ServiceError(
                        "operation_id was already used with a different request"
                    )
                if record["status"] == "completed":
                    return json_loads(record["response_json"], {})
                raise ServiceError(
                    "The same operation_id is already in progress; inspect status "
                    "before deciding whether recovery is required"
                )
            connection.execute(
                "INSERT INTO idempotency_records(operation, operation_id, "
                "request_sha256, status, created_at) VALUES (?, ?, ?, 'in_progress', ?)",
                (operation, key, request_hash, utc_now()),
            )
        try:
            response = callback()
        except Exception:
            self.db.execute(
                "DELETE FROM idempotency_records WHERE operation = ? "
                "AND operation_id = ? AND status = 'in_progress'",
                (operation, key),
            )
            raise
        self.db.execute(
            "UPDATE idempotency_records SET status = 'completed', response_json = ?, "
            "completed_at = ? WHERE operation = ? AND operation_id = ? "
            "AND status = 'in_progress'",
            (json_dumps(response), utc_now(), operation, key),
        )
        return response

    def status(self, workflow_id: str | None = None) -> dict[str, Any]:
        workflow = (
            self.db.one("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
            if workflow_id
            else self.db.one("SELECT * FROM workflows ORDER BY created_at DESC LIMIT 1")
        )
        active_run = self.db.one(
            """
            SELECT * FROM runs
            WHERE status IN ('starting', 'running', 'stopping')
            ORDER BY started_at DESC LIMIT 1
            """
        )
        lock = None
        if self.settings.lock_path.is_file():
            try:
                lock = json.loads(self.settings.lock_path.read_text(encoding="utf-8"))
            except Exception:
                lock = {"status": "unreadable"}
        result: dict[str, Any] = {
            "server": {
                "endpoint": f"http://{self.settings.host}:{self.settings.port}{self.settings.mcp_path}",
                "approval_endpoint": self.browser_approval_origin,
                "approval_transport": self.approval_transport,
                "remote_approval_enabled": self.remote_approval_enabled,
                "recovery_only": os.getenv("JY_RECOVERY_ONLY", "0") == "1",
                "qualibrate_config": str(self.settings.qualibrate_config_path),
                "qualibrate_project": self.settings.qualibrate_project,
                "active_state": str(self.settings.active_state),
                "wiring": str(self.settings.wiring_path),
                "data_root": str(self.settings.data_root),
                "operator_console": self.operator_console_url,
            },
            "measurement_mode": {
                "entry_phrase": self.settings.measurement_mode_entry_phrase,
                "entry_phrases": list(
                    self.settings.measurement_mode_entry_phrases
                ),
                "exit_phrase": self.settings.measurement_mode_exit_phrase,
                "approval_model": "per_run_and_state_commit",
                "workflow_active": bool(
                    workflow and workflow["status"] == "active"
                ),
            },
            "autonomy_mode": {
                "entry_phrase": self.settings.autonomy_mode_entry_phrase,
                "entry_phrases": list(
                    self.settings.autonomy_mode_entry_phrases
                ),
                "approval_model": "bounded_lease",
            },
            "recovery_phrases": list(self.settings.recovery_phrases),
            "measurement_resume_phrases": list(
                self.settings.measurement_resume_phrases
            ),
            "hardware_lock": lock,
            "active_run": self._decode_run(active_run) if active_run else None,
        }
        if workflow:
            result["workflow"] = self._decode_workflow(workflow)
            result["recent_runs"] = [
                self._decode_run(item)
                for item in self.db.all(
                    "SELECT * FROM runs WHERE workflow_id = ? ORDER BY started_at DESC LIMIT 10",
                    (workflow["id"],),
                )
            ]
            lease = self.db.one(
                "SELECT * FROM autonomy_leases WHERE workflow_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (workflow["id"],),
            )
            result["autonomy"] = (
                self.autonomy_status(lease_id=lease["id"]) if lease else None
            )
        else:
            result["workflow"] = None
            result["autonomy"] = None
        return result

    def run_telemetry(
        self,
        node_id: str | None = None,
        targets: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return persisted run parameters/timing and a robust duration estimate."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ServiceError("limit must be between 1 and 500")
        query = "SELECT * FROM runs"
        params: tuple[Any, ...] = ()
        if node_id is not None:
            self.policy.node_definition(node_id)
            query += " WHERE node_id = ?"
            params = (node_id,)
        rows = self.db.all(query + " ORDER BY started_at DESC LIMIT ?", (*params, limit))
        requested = set(targets or [])
        history: list[dict[str, Any]] = []
        for row in rows:
            decoded = self._decode_run(row)
            run_targets = set(decoded["parameters"].get("qubits", []))
            if requested and run_targets != requested:
                continue
            history.append(
                {
                    "run_id": decoded["id"],
                    "node_id": decoded["node_id"],
                    "targets": sorted(run_targets),
                    "parameters": decoded["parameters"],
                    "status": decoded["status"],
                    "started_at": decoded.get("started_at"),
                    "finished_at": decoded.get("finished_at"),
                    "elapsed_seconds": decoded.get("elapsed_seconds"),
                    "snapshot_id": decoded.get("snapshot_id"),
                    "analysis_status": decoded.get("analysis_status"),
                }
            )
        durations = [
            float(item["elapsed_seconds"])
            for item in history
            if item["status"] == "completed"
            and isinstance(item.get("elapsed_seconds"), (int, float))
        ]
        prediction = None
        if durations:
            ordered = sorted(durations)
            prediction = {
                "basis": "completed runs with the same node and exact targets"
                if requested
                else "completed runs for the selected node",
                "sample_size": len(ordered),
                "median_seconds": statistics.median(ordered),
                "p80_seconds": ordered[min(len(ordered) - 1, math.ceil(0.8 * len(ordered)) - 1)],
                "minimum_seconds": ordered[0],
                "maximum_seconds": ordered[-1],
            }
        return {"prediction": prediction, "history": history}

    def decision_experience(
        self,
        node_id: str | None = None,
        targets: list[str] | None = None,
        limit: int = 10,
        include_analysis: bool = False,
    ) -> list[dict[str, Any]]:
        """Expose recorded scientific decisions as reusable audit experience.

        Operator instruction 2026-09-20: each entry used to carry the run's
        whole analysis document, and the default asked for fifty of them, which
        is far more context than choosing the next parameters needs. Each entry
        now carries `evidence`, a per-target digest of the numbers the playbook
        actually gates on. Pass `include_analysis=True` for the full document.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ServiceError("limit must be between 1 and 200")
        query = (
            "SELECT d.*, r.node_id, r.parameters_json, r.analysis_json, "
            "r.analysis_status, r.snapshot_id, r.snapshot_path, r.started_at, "
            "r.finished_at FROM decisions d JOIN runs r ON r.id = d.run_id"
        )
        params: tuple[Any, ...] = ()
        if node_id is not None:
            self.policy.node_definition(node_id)
            query += " WHERE r.node_id = ?"
            params = (node_id,)
        rows = self.db.all(query + " ORDER BY d.created_at DESC LIMIT ?", (*params, limit))
        requested = set(targets or [])
        result: list[dict[str, Any]] = []
        for row in rows:
            parameters = json_loads(row["parameters_json"], {})
            run_targets = set(parameters.get("qubits", []))
            if requested and run_targets != requested:
                continue
            analysis = json_loads(row.get("analysis_json"), None)
            entry = {
                "decision_id": row["id"],
                "workflow_id": row["workflow_id"],
                "run_id": row["run_id"],
                "node_id": row["node_id"],
                "targets": sorted(run_targets),
                "parameters": parameters,
                "elapsed_seconds": _elapsed_seconds(row),
                "snapshot_id": row.get("snapshot_id"),
                "snapshot_path": row.get("snapshot_path"),
                "analysis_status": row.get("analysis_status"),
                "evidence": evidence_digest(analysis),
                "Decision": row["decision"],
                "Reason": row["reason"],
                "next_action": {
                    "next_node": row.get("next_node"),
                    "new_parameters": json_loads(row["next_parameters_json"], {}),
                },
                "state_patch": json_loads(row["state_patch_json"], []),
                "created_at": row["created_at"],
            }
            if include_analysis:
                entry["analysis"] = analysis
            result.append(entry)
        return result

    def start_workflow(
        self,
        targets: list[str],
        initial_parameters: dict[str, Any] | None,
        client_id: str,
        activation_phrase: str,
    ) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        if activation_phrase not in {
            *self.settings.measurement_mode_entry_phrases,
            *self.settings.autonomy_mode_entry_phrases,
        }:
            raise ServiceError(
                "Starting a workflow requires the exact measurement-mode or "
                "autonomy-mode entry phrase"
            )
        state = load_state(self.settings.active_state)
        if not targets or any(name not in state.get("qubits", {}) for name in targets):
            raise ServiceError("targets must be explicit qubits present in active state")
        if len(set(targets)) != len(targets):
            raise ServiceError("targets must not contain duplicates")
        workflow_parameters = initial_parameters or {}
        if not isinstance(workflow_parameters, dict):
            raise ServiceError("initial_parameters must be an object")
        multiplexed = workflow_parameters.get("multiplexed", False)
        if not isinstance(multiplexed, bool):
            raise ServiceError("workflow multiplexed must be a boolean")
        if multiplexed and len(targets) > 1:
            self.policy.validate_multiplex_targets(targets, state)
        workflow_id = uuid.uuid4().hex
        now = utc_now()
        first_node = self.settings.workflow_sequence[0]
        try:
            with self.db.transaction(immediate=True) as connection:
                existing = connection.execute(
                    "SELECT id FROM workflows WHERE status IN ('active', 'paused') "
                    "LIMIT 1"
                ).fetchone()
                if existing is not None:
                    raise ServiceError(
                        "An active or paused workflow already exists; resume or "
                        "finish it before starting another. If it is stale, issue "
                        "'恢復' or 'Recover' to close the prior JY state safely"
                    )
                connection.execute(
                    """
                    INSERT INTO workflows(
                        id, created_at, updated_at, status, targets_json,
                        initial_parameters_json, current_node, client_id
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        now,
                        now,
                        json_dumps(targets),
                        json_dumps(workflow_parameters),
                        first_node,
                        client_id,
                    ),
                )
                self.db.event(
                    "workflow_started",
                    client_id,
                    {
                        "targets": targets,
                        "current_node": first_node,
                        "multiplexed": multiplexed,
                    },
                    workflow_id,
                    connection=connection,
                )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(
                "Another client started an active workflow concurrently"
            ) from exc
        return self._decode_workflow(
            self.db.one("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
        )

    def request_autonomy_lease(
        self,
        workflow_id: str,
        client_id: str,
        activation_phrase: str,
        reason: str,
        targets: list[str] | None = None,
        allowed_nodes: list[str] | None = None,
        duration_hours: float | None = None,
    ) -> dict[str, Any]:
        """Create the one human approval that bounds unattended JY actions."""
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        if activation_phrase not in self.settings.autonomy_mode_entry_phrases:
            raise ServiceError(
                "Autonomy requires the exact autonomous-measurement entry phrase"
            )
        if not reason.strip():
            raise ServiceError("An autonomy authorization reason is required")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active":
            raise ServiceError("Workflow must be active before requesting autonomy")
        existing = self.db.one(
            "SELECT * FROM autonomy_leases WHERE workflow_id = ? "
            "AND status IN ('pending', 'active', 'paused') "
            "ORDER BY created_at DESC LIMIT 1",
            (workflow_id,),
        )
        if existing is not None:
            existing = self._expire_autonomy_if_needed(existing)
            if existing["status"] in {"pending", "active", "paused"}:
                raise ServiceError(
                    "Workflow already has a pending or usable autonomy lease: "
                    f"{existing['id']} ({existing['status']})"
                )

        workflow = self._rewind_invalidated_subgroup_evidence(
            workflow, client_id
        )

        workflow_targets = json_loads(workflow["targets_json"], [])
        resolved_targets = targets or list(workflow_targets)
        if (
            not resolved_targets
            or len(set(resolved_targets)) != len(resolved_targets)
            or not set(resolved_targets).issubset(set(workflow_targets))
        ):
            raise ServiceError(
                "Autonomy targets must be a non-empty subset of workflow targets"
            )
        sequence = list(self.settings.workflow_sequence)
        current_index = sequence.index(workflow["current_node"])
        remaining_nodes = sequence[current_index:]
        resolved_nodes = allowed_nodes or remaining_nodes
        if (
            not resolved_nodes
            or len(set(resolved_nodes)) != len(resolved_nodes)
            or resolved_nodes[0] != workflow["current_node"]
            or any(node not in remaining_nodes for node in resolved_nodes)
            or [node for node in remaining_nodes if node in resolved_nodes]
            != resolved_nodes
        ):
            raise ServiceError(
                "Autonomy nodes must be an ordered subset of the remaining workflow "
                "and start at the current node"
            )
        config = self.policy.raw["autonomy"]
        duration_hours = self._resolve_autonomy_duration(duration_hours)
        max_total_runs = config.get("max_total_runs")
        max_attempts = int(config["max_attempts_per_node_qubit"])
        lease_id = uuid.uuid4().hex
        payload = {
            "lease_id": lease_id,
            "reason": reason.strip(),
            "targets": resolved_targets,
            "allowed_nodes": resolved_nodes,
            "duration_hours": duration_hours,
            "max_total_runs": max_total_runs,
            "max_attempts_per_node_qubit": max_attempts,
            "auto_state_commit_analysis_statuses": list(
                config["auto_state_commit_analysis_statuses"]
            ),
            "pause_on_analysis_statuses": list(
                config.get("pause_on_analysis_statuses", [])
            ),
            "halt_conditions": list(config["halt_conditions"]),
            "manual_controls": ["pause", "stop", "emergency_stop"],
        }
        proposal = self._create_proposal(
            workflow_id,
            "autonomy_lease",
            payload,
            client_id,
            int(self.policy.raw["approval"]["run_ttl_minutes"]),
            1,
            autonomy_lease_id=lease_id,
        )
        now = utc_now()
        try:
            self.db.execute(
                """
                INSERT INTO autonomy_leases(
                    id, workflow_id, proposal_id, status, targets_json,
                    allowed_nodes_json, duration_hours, max_total_runs,
                    max_attempts_per_node_qubit, auto_state_commit_statuses_json,
                    halt_conditions_json, created_at, updated_at, source_client
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    workflow_id,
                    proposal["id"],
                    json_dumps(resolved_targets),
                    json_dumps(resolved_nodes),
                    duration_hours,
                    max_total_runs,
                    max_attempts,
                    json_dumps(config["auto_state_commit_analysis_statuses"]),
                    json_dumps(config["halt_conditions"]),
                    now,
                    now,
                    client_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            self.db.execute(
                "UPDATE proposals SET status = 'cancelled' WHERE id = ? "
                "AND status = 'pending'",
                (proposal["id"],),
            )
            raise ServiceError(
                "Another client created a pending or active autonomy lease concurrently"
            ) from exc
        self.db.event(
            "autonomy_lease_requested",
            client_id,
            payload,
            workflow_id,
        )
        dashboard = self._start_dashboard_session(
            workflow_id,
            "autonomous",
            client_id,
            autonomy_lease_id=lease_id,
        )
        # Decode the proposal only after the workflow's stable Dashboard session
        # is bound to this lease.  Direct MCP callers of request_autonomy_lease
        # must receive the same canonical browser page as enter_autonomy_mode.
        proposal = self.proposal(proposal["id"])
        proposal["autonomy"] = self.autonomy_status(lease_id=lease_id)
        proposal["dashboard"] = dashboard
        proposal["browser_url"] = dashboard["browser_url"]
        return proposal

    def enter_measurement_mode(
        self,
        client_id: str,
        activation_phrase: str,
        targets: list[str] | None = None,
        *,
        multiplexed: bool | None = None,
    ) -> dict[str, Any]:
        """Start or resume conversational, one-experiment-at-a-time mode."""
        if activation_phrase not in self.settings.measurement_mode_entry_phrases:
            raise ServiceError(
                "Entry requires the exact JY measurement-mode phrase"
            )
        self._reject_retained_hardware_lock()
        workflow = self._enter_or_resume_workflow(
            client_id,
            activation_phrase,
            targets,
            multiplexed=multiplexed,
            allow_live_autonomy=False,
        )
        dashboard = self._start_dashboard_session(
            workflow["id"], "conversational", client_id
        )
        return {
            "workflow": workflow,
            "mode": "conversational",
            "approval_model": "per_run_and_state_commit",
            "next_action": "discuss_and_propose_one_experiment",
            "current_node": workflow["current_node"],
            "dashboard": dashboard,
            "browser_url": dashboard["browser_url"],
            "operator_handoff": approval_operator_handoff(),
        }

    def experiment_catalog(self) -> dict[str, Any]:
        """List policy-runnable experiments and scripts awaiting safe onboarding."""
        registered: list[dict[str, Any]] = []
        registered_paths: set[Path] = set()
        sequence = set(self.settings.workflow_sequence)
        for node_id, definition in sorted(self.policy.nodes.items()):
            script = self.policy.node_script(node_id)
            registered_paths.add(script.resolve())
            registered.append(
                {
                    "node_id": node_id,
                    "name": definition["name"],
                    "script": str(script),
                    "automatic_sequence": node_id in sequence,
                    "conversational_available": True,
                    "allowed_parameters": definition.get(
                        "allowed_parameters", {}
                    ),
                    "defaults": definition.get("defaults", {}),
                }
            )

        roots = {
            "calibration_graph": self.settings.calibration_graph,
            "Script": self.settings.superconducting_root / "Script",
            "side_project": self.settings.superconducting_root / "side_project",
        }
        awaiting: list[dict[str, str]] = []
        for root_name, root in roots.items():
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts or path.resolve() in registered_paths:
                    continue
                awaiting.append(
                    {
                        "root": root_name,
                        "script": path.relative_to(root).as_posix(),
                        "status": "policy_onboarding_required",
                    }
                )
        return {
            "registered": registered,
            "registered_count": len(registered),
            "awaiting_policy_onboarding": awaiting,
            "awaiting_count": len(awaiting),
            "safety_note": (
                "Conversational mode can run every registered node, but never "
                "executes an unregistered Python file. Add a policy schema, limits, "
                "and analyzer before a discovered script becomes runnable."
            ),
        }

    def enter_autonomy_mode(
        self,
        client_id: str,
        activation_phrase: str,
        targets: list[str] | None = None,
        *,
        multiplexed: bool | None = None,
        duration_hours: float | None = None,
        reason: str = "Operator entered JY autonomous measurement mode.",
    ) -> dict[str, Any]:
        """Start/resume a workflow and obtain its bounded autonomy lease.

        `duration_hours` sets this session's budget; omitting it uses the
        configured default. It applies only when a new lease is created -- an
        existing pending, active, or paused lease keeps the hours it was
        approved with.
        """
        if activation_phrase not in self.settings.autonomy_mode_entry_phrases:
            raise ServiceError(
                "Entry requires the exact JY autonomous-measurement phrase"
            )
        # Reject an out-of-policy budget before a workflow is created.
        self._resolve_autonomy_duration(duration_hours)
        self._reject_retained_hardware_lock()
        workflow = self._enter_or_resume_workflow(
            client_id,
            activation_phrase,
            targets,
            multiplexed=multiplexed,
            allow_live_autonomy=True,
        )
        now = utc_now()
        self.db.execute(
            "UPDATE proposals SET status = 'expired' "
            "WHERE workflow_id = ? AND status = 'pending' "
            "AND autonomy_lease_id IS NULL AND expires_at <= ?",
            (workflow["id"], now),
        )
        pending_manual = self.db.one(
            "SELECT id FROM proposals WHERE workflow_id = ? AND status = 'pending' "
            "AND autonomy_lease_id IS NULL AND kind != 'autonomy_lease' "
            "ORDER BY created_at DESC LIMIT 1",
            (workflow["id"],),
        )
        if pending_manual is not None:
            raise ServiceError(
                "Automatic mode cannot start while a conversational proposal is "
                "pending; approve, expire, or cancel that proposal first"
            )

        lease_row = self.db.one(
            "SELECT * FROM autonomy_leases WHERE workflow_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (workflow["id"],),
        )
        if lease_row is not None:
            lease = self.autonomy_status(lease_id=lease_row["id"])
            if lease["status"] == "pending":
                result = {
                    "workflow": workflow,
                    "mode": "autonomous",
                    "authorization": lease,
                    "approval": self.proposal(lease["proposal_id"]),
                    "next_action": "human_approval",
                }
                return self._with_autonomy_dashboard(result, client_id)
            if lease["status"] == "active":
                result = {
                    "workflow": workflow,
                    "mode": "autonomous",
                    "authorization": lease,
                    "next_action": "continue_authorized_workflow",
                }
                return self._with_autonomy_dashboard(result, client_id)
            if lease["status"] == "paused":
                lease = self.resume_autonomy(
                    lease["id"], client_id, activation_phrase
                )
                result = {
                    "workflow": workflow,
                    "mode": "autonomous",
                    "authorization": lease,
                    "next_action": "continue_authorized_workflow",
                }
                return self._with_autonomy_dashboard(result, client_id)

        proposal = self.request_autonomy_lease(
            workflow["id"],
            client_id,
            activation_phrase,
            reason or "Operator entered JY autonomous measurement mode.",
            duration_hours=duration_hours,
        )
        result = {
            "workflow": workflow,
            "mode": "autonomous",
            "authorization": proposal["autonomy"],
            "approval": proposal,
            "next_action": "human_approval",
        }
        return self._with_autonomy_dashboard(result, client_id)

    def _with_autonomy_dashboard(
        self, result: dict[str, Any], client_id: str
    ) -> dict[str, Any]:
        lease_id = str(result["authorization"]["id"])
        dashboard = self._start_dashboard_session(
            str(result["authorization"]["workflow_id"]),
            "autonomous",
            client_id,
            autonomy_lease_id=lease_id,
        )
        result["authorization"] = self.autonomy_status(lease_id=lease_id)
        approval_id = str((result.get("approval") or {}).get("id") or "")
        if approval_id:
            result["approval"] = self.proposal(approval_id)
        result["dashboard"] = dashboard
        result["browser_url"] = dashboard["browser_url"]
        if result.get("next_action") == "human_approval":
            result["operator_handoff"] = approval_operator_handoff()
        return result

    def _active_qubit_names_from_state(self) -> list[str]:
        state = load_state(self.settings.active_state)
        names = state.get("active_qubit_names")
        if not isinstance(names, list) or not names:
            raise ServiceError(
                "A new workflow needs targets; none were given and "
                "state.json active_qubit_names is missing or empty"
            )
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ServiceError("active_qubit_names must be a list of qubit names")
        if len(set(names)) != len(names):
            raise ServiceError("active_qubit_names must not contain duplicates")
        return list(names)

    def _enter_or_resume_workflow(
        self,
        client_id: str,
        activation_phrase: str,
        targets: list[str] | None,
        *,
        multiplexed: bool | None,
        allow_live_autonomy: bool,
    ) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")

        row = self.db.one(
            "SELECT * FROM workflows WHERE status IN ('active', 'paused') "
            "ORDER BY created_at DESC LIMIT 1"
        )
        if row is None:
            resolved_targets = list(targets) if targets else self._active_qubit_names_from_state()
            resolved_multiplexed = True if multiplexed is None else bool(multiplexed)
            workflow = self.start_workflow(
                resolved_targets,
                {"multiplexed": resolved_multiplexed},
                client_id,
                activation_phrase,
            )
        else:
            workflow = self._decode_workflow(row)
            if targets is not None and list(targets) != workflow["targets"]:
                raise ServiceError(
                    "Requested targets differ from the existing workflow; stop or "
                    "finish it before creating a different authorization scope. "
                    "If the prior workflow is stale, issue '恢復' or 'Recover'"
                )
            existing_multiplexed = bool(
                workflow["initial_parameters"].get("multiplexed", False)
            )
            if multiplexed is True and not existing_multiplexed:
                raise ServiceError(
                    "The existing workflow was not created as multiplexed"
                )
            if not allow_live_autonomy:
                lease_row = self.db.one(
                    "SELECT * FROM autonomy_leases WHERE workflow_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (workflow["id"],),
                )
                if lease_row is not None:
                    lease = self.autonomy_status(lease_id=lease_row["id"])
                    if lease["status"] in {"pending", "active", "paused"}:
                        raise ServiceError(
                            "Conversational mode cannot share a live autonomy lease; "
                            "stop or revoke that lease first"
                        )
            if workflow["status"] == "paused":
                workflow = self.resume_measurement_mode(
                    workflow["id"], client_id, activation_phrase
                )
        return workflow

    def autonomy_status(
        self,
        lease_id: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        if not lease_id and not workflow_id:
            raise ServiceError("lease_id or workflow_id is required")
        if lease_id:
            row = self.db.one("SELECT * FROM autonomy_leases WHERE id = ?", (lease_id,))
        else:
            row = self.db.one(
                "SELECT * FROM autonomy_leases WHERE workflow_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (workflow_id,),
            )
        if row is None:
            raise ServiceError("Unknown autonomy lease")
        row = self._expire_autonomy_if_needed(row)
        return self._decode_autonomy(row)

    def wait_for_autonomy_status(
        self,
        lease_id: str,
        *,
        after_event_id: int = 0,
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        """Wait briefly for approval, a result/control event, or terminal lease state."""
        timeout = max(0.0, min(float(timeout_seconds), 55.0))
        cursor = max(0, int(after_event_id))
        deadline = time.monotonic() + timeout
        initial_status = self.autonomy_status(lease_id=lease_id)["status"]
        while True:
            status = self.autonomy_status(lease_id=lease_id)
            events = self.autonomy_ui_events_after(lease_id, cursor)
            if (
                events["events"]
                or status["status"] != initial_status
                or status["status"] not in {"pending", "active"}
            ):
                return self._with_wait_operator_handoff(
                    {
                        "timed_out": False,
                        "authorization": status,
                        "cursor": events["cursor"],
                        "events": events["events"],
                    }
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._with_wait_operator_handoff(
                    {
                        "timed_out": True,
                        "authorization": status,
                        "cursor": events["cursor"],
                        "events": [],
                    }
                )
            time.sleep(min(0.25, remaining))

    def _with_wait_operator_handoff(self, result: dict[str, Any]) -> dict[str, Any]:
        status = str((result.get("authorization") or {}).get("status") or "")
        if status == "pending":
            result["operator_handoff"] = approval_operator_handoff()
        elif status == "paused":
            result["operator_handoff"] = resume_autonomy_operator_handoff()
        return result

    def mark_scientifically_unmeasurable(
        self,
        workflow_id: str,
        lease_id: str,
        run_id: str,
        targets: list[str],
        reason: str,
        client_id: str,
    ) -> dict[str, Any]:
        """Mark evidence-reviewed targets incomplete before their attempt quota."""
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        if not reason.strip():
            raise ServiceError("A scientific boundary reason is required")
        lease = self._active_autonomy(lease_id, workflow_id)
        workflow = self._workflow(workflow_id)
        run = self.db.one(
            "SELECT * FROM runs WHERE id = ? AND workflow_id = ?",
            (run_id, workflow_id),
        )
        if run is None or run.get("autonomy_lease_id") != lease["id"]:
            raise ServiceError("Run is not associated with this autonomy lease")
        if run["node_id"] != workflow["current_node"]:
            raise ServiceError("Scientific boundary evidence must belong to the current node")
        if run["status"] != "completed" or run["analysis_status"] not in {
            "needs_review",
            "failed",
        }:
            raise ServiceError(
                "Scientific incompleteness requires a completed non-passing analysis"
            )
        decision = self.db.one(
            "SELECT decision FROM decisions WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        )
        if decision is None or decision["decision"] != "manual_review":
            raise ServiceError(
                "Record a manual_review decision for the evidence run before "
                "marking a target scientifically unmeasurable"
            )
        normalized_targets = {str(target) for target in targets if str(target)}
        if not normalized_targets:
            raise ServiceError("At least one scientifically unmeasurable target is required")
        run_targets = set(
            json_loads(run.get("parameters_json"), {}).get("qubits", [])
        )
        if not normalized_targets.issubset(run_targets):
            raise ServiceError("Targets must be a subset of the evidence run targets")
        active_targets = self._active_targets_for_node(workflow, run["node_id"])
        if not normalized_targets.issubset(active_targets):
            raise ServiceError("Targets are outside the active workflow target set")
        resolved = self._node_target_resolution(workflow_id, run["node_id"])
        already_resolved = normalized_targets & resolved
        if already_resolved:
            raise ServiceError(
                f"Resolved targets cannot be marked incomplete: {sorted(already_resolved)}"
            )
        self.db.event(
            "autonomy_targets_scientifically_unmeasurable",
            client_id,
            {
                "lease_id": lease_id,
                "run_id": run_id,
                "node_id": run["node_id"],
                "targets": sorted(normalized_targets),
                "reason": reason.strip(),
            },
            workflow_id,
        )
        return {
            "workflow_id": workflow_id,
            "lease_id": lease_id,
            "run_id": run_id,
            "node_id": run["node_id"],
            "targets": sorted(normalized_targets),
            "status": "scientifically_unmeasurable",
            "reason": reason.strip(),
            "incomplete_targets_by_node": self.autonomy_status(
                lease_id=lease_id
            )["incomplete_targets_by_node"],
        }

    def pause_autonomy(
        self, lease_id: str, client_id: str, reason: str
    ) -> dict[str, Any]:
        return self._set_autonomy_control(
            lease_id, client_id, reason, action="pause"
        )

    def resume_autonomy(
        self,
        lease_id: str,
        client_id: str,
        activation_phrase: str,
    ) -> dict[str, Any]:
        if activation_phrase not in {
            *self.settings.autonomy_mode_entry_phrases,
            *self.settings.measurement_resume_phrases,
        }:
            raise ServiceError(
                "Resuming autonomy requires an exact resume or "
                "autonomous-measurement phrase"
            )
        self._reject_retained_hardware_lock()
        row = self._autonomy_row(lease_id)
        row = self._expire_autonomy_if_needed(row)
        if row["status"] != "paused":
            raise ServiceError("Only a paused, unexpired autonomy lease can resume")
        self.db.execute(
            "UPDATE autonomy_leases SET status = 'active', stopped_reason = NULL, "
            "updated_at = ? WHERE id = ?",
            (utc_now(), lease_id),
        )
        self.db.execute(
            "UPDATE measurement_sessions SET status = 'active', updated_at = ? "
            "WHERE autonomy_lease_id = ? AND status = 'paused'",
            (utc_now(), lease_id),
        )
        self.db.event(
            "autonomy_resumed", client_id, {"lease_id": lease_id}, row["workflow_id"]
        )
        return self.autonomy_status(lease_id=lease_id)

    def stop_autonomy(
        self,
        lease_id: str,
        client_id: str,
        reason: str,
        *,
        emergency: bool = False,
    ) -> dict[str, Any]:
        return self._set_autonomy_control(
            lease_id,
            client_id,
            reason,
            action="emergency_stop" if emergency else "stop",
        )

    def leave_measurement_mode(
        self,
        workflow_id: str,
        client_id: str,
        exit_phrase: str,
    ) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        if exit_phrase not in self.settings.measurement_mode_shutdown_phrases:
            raise ServiceError(
                "Leaving measurement mode requires a configured shutdown phrase"
            )
        return self.stop_workflow(
            workflow_id,
            client_id,
            f"Operator issued shutdown phrase: {exit_phrase}",
        )

    def resume_measurement_mode(
        self,
        workflow_id: str,
        client_id: str,
        activation_phrase: str,
    ) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        if activation_phrase not in {
            *self.settings.measurement_resume_phrases,
            *self.settings.measurement_mode_entry_phrases,
            *self.settings.autonomy_mode_entry_phrases,
        }:
            raise ServiceError(
                "Resuming requires an exact measurement-resume or mode-entry phrase"
            )
        self._reject_retained_hardware_lock()
        active_run = self.db.one(
            "SELECT id FROM runs WHERE workflow_id = ? "
            "AND status IN ('starting', 'running', 'stopping') LIMIT 1",
            (workflow_id,),
        )
        if active_run is not None:
            raise ServiceError("The workflow already has an active worker")
        workflow = self._workflow(workflow_id)
        lease_row = self.db.one(
            "SELECT * FROM autonomy_leases WHERE workflow_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (workflow_id,),
        )
        lease = (
            self._expire_autonomy_if_needed(lease_row)
            if lease_row is not None
            else None
        )
        paused_lease = lease if lease and lease["status"] == "paused" else None
        if workflow["status"] != "paused" and paused_lease is None:
            raise ServiceError("Only a paused workflow or authorization can be resumed")
        now = utc_now()
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE workflows SET status = 'active', updated_at = ? "
                "WHERE id = ? AND status = 'paused'",
                (now, workflow_id),
            )
            if paused_lease is not None:
                connection.execute(
                    "UPDATE autonomy_leases SET status = 'active', "
                    "stopped_reason = NULL, updated_at = ? "
                    "WHERE id = ? AND status = 'paused'",
                    (now, paused_lease["id"]),
                )
            connection.execute(
                "UPDATE measurement_sessions SET status = 'active', updated_at = ? "
                "WHERE workflow_id = ? AND status = 'paused'",
                (now, workflow_id),
            )
            self.db.event(
                "measurement_mode_resumed",
                client_id,
                {
                    "workflow_id": workflow_id,
                    "activation_phrase": activation_phrase,
                    "autonomy_lease_id": (
                        paused_lease["id"] if paused_lease is not None else None
                    ),
                    "resume_policy": "restart_current_node_as_new_run",
                },
                workflow_id,
                connection=connection,
            )
        result = self._decode_workflow(self._workflow(workflow_id))
        result["resume_policy"] = "restart_current_node_as_new_run"
        result["authorization"] = (
            self.autonomy_status(lease_id=str(paused_lease["id"]))
            if paused_lease is not None
            else None
        )
        session_id = self.dashboard_session_id_for_workflow(workflow_id)
        result["dashboard"] = (
            self.dashboard_status(session_id) if session_id is not None else None
        )
        return result

    def reopen_07b_after_morphology_rule_change(
        self,
        workflow_id: str,
        run_id: str,
        reason: str,
        client_id: str,
    ) -> dict[str, Any]:
        """Backtrack only one node when accepted 07b evidence is now invalid."""
        if not client_id.strip() or not reason.strip():
            raise ServiceError("07b reopening requires actor and reason")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active" or workflow["current_node"] != "06":
            raise ServiceError("07b may be reopened only from the immediately following 06")
        active_run = self.db.one(
            "SELECT id FROM runs WHERE workflow_id = ? "
            "AND status IN ('starting', 'running', 'stopping') LIMIT 1",
            (workflow_id,),
        )
        if active_run is not None:
            raise ServiceError("Wait for or stop the active run before reopening 07b")
        run = self.db.one(
            "SELECT * FROM runs WHERE id = ? AND workflow_id = ? AND node_id = '07b'",
            (run_id, workflow_id),
        )
        if (
            run is None
            or run["status"] != "completed"
            or run["analysis_status"] == "pass"
        ):
            raise ServiceError(
                "Reopening requires a completed 07b run reanalyzed as non-passing"
            )
        analysis = json_loads(run.get("analysis_json"), {})
        morphology = analysis.get("dataset_metrics", {}).get("qubits", {})
        targets = json_loads(run.get("parameters_json"), {}).get("qubits", [])
        invalid = [
            name
            for name in targets
            if isinstance(morphology.get(name), dict)
            and morphology[name].get("morphology_pass") is False
        ]
        if not invalid:
            raise ServiceError("Reanalyzed 07b run has no failed cloud morphology evidence")

        leases = self.db.all(
            "SELECT * FROM autonomy_leases WHERE workflow_id = ? "
            "AND status IN ('pending', 'active', 'paused')",
            (workflow_id,),
        )
        for lease in leases:
            lease = self._expire_autonomy_if_needed(lease)
            if lease["status"] in {"active", "paused"}:
                raise ServiceError(
                    "Stop the active/paused autonomy lease before reopening 07b"
                )
            if lease["status"] == "pending":
                self.db.execute(
                    "UPDATE autonomy_leases SET status = 'revoked', stopped_reason = ?, "
                    "updated_at = ? WHERE id = ?",
                    (reason.strip(), utc_now(), lease["id"]),
                )
                self.db.execute(
                    "UPDATE proposals SET status = 'cancelled' WHERE id = ? "
                    "AND status = 'pending'",
                    (lease["proposal_id"],),
                )
        self.db.execute(
            "UPDATE workflows SET current_node = '07b', updated_at = ? WHERE id = ?",
            (utc_now(), workflow_id),
        )
        result = {
            "workflow_id": workflow_id,
            "reopened_node": "07b",
            "invalidated_run_id": run_id,
            "morphology_failed_targets": invalid,
            "reason": reason.strip(),
        }
        self.db.event("07b_reopened", client_id, result, workflow_id)
        result["workflow"] = self._decode_workflow(self._workflow(workflow_id))
        return result

    def stop_workflow(
        self,
        workflow_id: str,
        client_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        if not reason.strip():
            raise ServiceError("A stop reason is required")
        workflow = self._workflow(workflow_id)
        if workflow["status"] not in {"active", "paused"}:
            raise ServiceError("Only an active or paused workflow can be stopped")
        active_run = self.db.one(
            """
            SELECT id FROM runs
            WHERE workflow_id = ?
              AND status IN ('starting', 'running', 'stopping')
            LIMIT 1
            """,
            (workflow_id,),
        )
        if active_run is not None:
            raise ServiceError(
                "Wait for or stop the active run before stopping the workflow"
            )
        recovery_required = self.settings.lock_path.exists()
        final_status = "recovery_required" if recovery_required else "stopped"
        self.db.execute(
            """
            UPDATE workflows SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (final_status, utc_now(), workflow_id),
        )
        self.db.execute(
            "UPDATE autonomy_leases SET status = 'revoked', stopped_reason = ?, "
            "updated_at = ? WHERE workflow_id = ? "
            "AND status IN ('pending', 'active', 'paused')",
            (reason.strip(), utc_now(), workflow_id),
        )
        self.db.execute(
            "UPDATE proposals SET status = 'cancelled' WHERE workflow_id = ? "
            "AND status = 'pending'",
            (workflow_id,),
        )
        self.db.execute(
            "UPDATE measurement_sessions SET status = ?, updated_at = ? "
            "WHERE workflow_id = ? AND status != 'stopped'",
            (final_status, utc_now(), workflow_id),
        )
        self.db.event(
            "workflow_quarantined" if recovery_required else "workflow_stopped",
            client_id,
            {
                "workflow_id": workflow_id,
                "reason": reason.strip(),
                "recovery_required": recovery_required,
            },
            workflow_id,
        )
        return self._decode_workflow(self._workflow(workflow_id))

    def prepare_recovery_close(self, actor: str) -> dict[str, Any]:
        """Safely converge stale JY state before the lifecycle wrapper stops services.

        This operation never clears a retained hardware lock.  It requests a
        cooperative stop from a live, authenticated worker and otherwise closes
        orphaned workflows, leases, proposals, and Dashboard sessions so a
        subsequent service stop is deterministic.
        """

        if not actor.strip():
            raise ServiceError("Recovery close requires an audit actor")
        active_run = self.db.one(
            "SELECT * FROM runs WHERE status IN ('starting','running','stopping') "
            "ORDER BY started_at DESC LIMIT 1"
        )
        if active_run is not None:
            pid = active_run.get("pid")
            if isinstance(pid, int) and self.runner.process_is_alive(pid):
                try:
                    if active_run["status"] != "stopping":
                        self.runner.stop(
                            str(active_run["id"]),
                            actor,
                            intent="full_shutdown",
                            reason="Recover command requested a fully closed JY state.",
                        )
                except Exception as exc:
                    return {
                        "status": "blocked",
                        "reason": (
                            "The active worker could not receive a verified cooperative "
                            f"stop request: {exc}"
                        ),
                        "run_id": active_run["id"],
                        "services_may_stop": False,
                    }
                return {
                    "status": "waiting_for_worker",
                    "reason": "A verified worker is stopping cooperatively.",
                    "run_id": active_run["id"],
                    "services_may_stop": False,
                }

            evidence = self.runner.exit_evidence(str(active_run["id"]))
            stop_intent = str(
                active_run.get("stop_intent")
                or evidence.get("stop_request", {}).get("intent")
                or ""
            )
            terminal_status = (
                "cancelled_by_shutdown" if stop_intent == "full_shutdown" else "failed"
            )
            termination_cause = str(
                evidence.get("exit_receipt", {}).get("termination_cause")
                or "worker_missing_during_recovery_close"
            )
            error = active_run.get("error") or (
                "The recorded worker is no longer alive; recovery close converted "
                "the stale run record to a terminal state."
            )
            self.db.execute(
                "UPDATE runs SET status = ?, finished_at = COALESCE(finished_at, ?), "
                "error = COALESCE(error, ?), termination_cause = ? WHERE id = ?",
                (
                    terminal_status,
                    utc_now(),
                    error,
                    termination_cause,
                    active_run["id"],
                ),
            )
            self.db.event(
                "stale_run_closed_by_recovery",
                actor,
                {
                    "run_id": active_run["id"],
                    "status": terminal_status,
                    "termination_cause": termination_cause,
                },
                active_run["workflow_id"],
            )

        recovery_required = self.settings.lock_path.exists()
        final_status = "recovery_required" if recovery_required else "stopped"
        now = utc_now()
        workflows = self.db.all(
            "SELECT id FROM workflows WHERE status IN "
            "('active','paused','recovery_required')"
        )
        with self.db.transaction(immediate=True) as connection:
            for workflow in workflows:
                workflow_id = str(workflow["id"])
                connection.execute(
                    "UPDATE workflows SET status = ?, updated_at = ? WHERE id = ?",
                    (final_status, now, workflow_id),
                )
                connection.execute(
                    "UPDATE measurement_sessions SET status = ?, updated_at = ? "
                    "WHERE workflow_id = ? AND status != 'stopped'",
                    (final_status, now, workflow_id),
                )
                connection.execute(
                    "UPDATE autonomy_leases SET status = 'revoked', stopped_reason = ?, "
                    "updated_at = ? WHERE workflow_id = ? "
                    "AND status IN ('pending','active','paused')",
                    (
                        "Recover command closed the JY measurement environment.",
                        now,
                        workflow_id,
                    ),
                )
                connection.execute(
                    "UPDATE proposals SET status = 'cancelled' WHERE workflow_id = ? "
                    "AND status = 'pending'",
                    (workflow_id,),
                )
                self.db.event(
                    "workflow_quarantined"
                    if recovery_required
                    else "workflow_stopped",
                    actor,
                    {
                        "workflow_id": workflow_id,
                        "reason": "Recover command requested a fully closed state.",
                        "recovery_required": recovery_required,
                    },
                    workflow_id,
                    connection=connection,
                )
        return {
            "status": "recovery_required" if recovery_required else "ready",
            "reason": (
                "No worker is alive. Services may close; the retained hardware "
                "lock remains in local safety quarantine."
                if recovery_required
                else "No worker is alive and all open JY workflow state is closed."
            ),
            "services_may_stop": True,
            "hardware_lock_retained": recovery_required,
            "closed_workflow_ids": [str(item["id"]) for item in workflows],
        }

    def request_run(
        self,
        workflow_id: str,
        node_id: str,
        parameters: dict[str, Any],
        reason: str,
        client_id: str,
        auto_retry_count: int = 0,
        autonomy_lease_id: str | None = None,
        *,
        conversational: bool = False,
    ) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        if not reason.strip():
            raise ServiceError("A run proposal reason is required")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active":
            raise ServiceError("Workflow is not active")
        autonomy_lease = (
            self._active_autonomy(autonomy_lease_id, workflow_id)
            if autonomy_lease_id is not None
            else None
        )
        if conversational and autonomy_lease_id is not None:
            raise ServiceError(
                "A conversational run cannot be delegated to an autonomy lease"
            )
        if conversational:
            lease_row = self.db.one(
                "SELECT * FROM autonomy_leases WHERE workflow_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (workflow_id,),
            )
            if lease_row is not None:
                lease = self.autonomy_status(lease_id=lease_row["id"])
                if lease["status"] in {"pending", "active", "paused"}:
                    raise ServiceError(
                        "Conversational runs cannot share a live autonomy lease"
                    )
        prerequisite_verification = (
            not conversational
            and node_id != workflow["current_node"]
            and self._is_active_reset_verification_node(workflow, node_id)
        )
        advances_completed_node = (
            not conversational
            and not prerequisite_verification
            and node_id != workflow["current_node"]
            and self._node_is_finished_and_fully_decided(workflow, node_id)
        )
        if (
            not conversational
            and node_id != workflow["current_node"]
            and not prerequisite_verification
            and not advances_completed_node
        ):
            raise ServiceError(
                f"Current workflow node is {workflow['current_node']}; record a decision first"
            )
        if advances_completed_node:
            previous_node = str(workflow["current_node"])
            self.db.execute(
                "UPDATE workflows SET current_node = ?, updated_at = ? "
                "WHERE id = ? AND status = 'active'",
                (node_id, utc_now(), workflow_id),
            )
            self.db.event(
                "workflow_node_advanced_after_boundary",
                client_id,
                {"from_node": previous_node, "to_node": node_id},
                workflow_id,
            )
            workflow = self._workflow(workflow_id)
        maximum_retries = int(
            self.policy.raw["approval"]["max_auto_retries_per_approval"]
        )
        if (
            not isinstance(auto_retry_count, int)
            or isinstance(auto_retry_count, bool)
            or not 0 <= auto_retry_count <= maximum_retries
        ):
            raise ServiceError(
                f"auto_retry_count must be between 0 and {maximum_retries}"
            )
        workflow_parameters = json_loads(
            workflow["initial_parameters_json"], {}
        )
        workflow_targets = set(json_loads(workflow["targets_json"], []))
        definition = self.policy.node_definition(node_id)
        supports_multiplexed = "multiplexed" in definition.get(
            "allowed_parameters", {}
        )
        if workflow_parameters.get("multiplexed") is True and supports_multiplexed:
            if parameters.get("multiplexed") is False:
                raise ServiceError(
                    "This workflow requires multiplexed=true for every node"
                )
            parameters = {**parameters, "multiplexed": True}
        try:
            merged, warnings = self.policy.validate_run(node_id, parameters)
        except PolicyError as exc:
            # Refused before anything reached the instrument, so the refusal is
            # itself the protection. Record it and keep the lease usable; the
            # agent corrects the parameter and proposes again.
            if autonomy_lease_id is not None:
                self._record_refused_autonomy_action(
                    autonomy_lease_id,
                    workflow_id,
                    "parameter_policy_violation",
                    f"{node_id}: {exc}",
                )
            raise
        if prerequisite_verification and not _is_active_reset(merged):
            raise ServiceError(
                "An off-sequence 05/06/06b verification run is allowed only "
                "for active-reset revalidation after 07b qualification"
            )
        requested_targets = set(merged["qubits"])
        try:
            if conversational:
                workflow_targets = set(json_loads(workflow["targets_json"], []))
                if not requested_targets or not requested_targets <= workflow_targets:
                    raise ServiceError(
                        "Conversational run qubits must be a non-empty subset of "
                        "the workflow targets"
                    )
            elif node_id in SUBGROUP_NODES:
                eligible_targets = self._active_targets_for_node(workflow, node_id)
                if not requested_targets or not requested_targets <= eligible_targets:
                    raise AutonomyScopeError(
                        f"{node_id} run qubits must be a non-empty subset of active "
                        f"workflow targets {sorted(eligible_targets)}"
                    )
            elif node_id in list(self.settings.workflow_sequence)[3:]:
                expected_targets = self._active_targets_for_node(workflow, node_id)
                if requested_targets != expected_targets:
                    raise ServiceError(
                        "Run qubits must exactly match the resolved active targets "
                        "carried into this node"
                    )
            elif requested_targets != workflow_targets:
                raise ServiceError(
                    "Run qubits must exactly match the workflow targets; start a "
                    "separate workflow for a different target set"
                )
            if not conversational and not prerequisite_verification:
                self._validate_single_pass_node(workflow, node_id, merged)
            if (
                workflow_parameters.get("multiplexed") is True
                and not prerequisite_verification
            ):
                self._validate_multiplex_cap(node_id, requested_targets)
                self._validate_shared_parameter_first_batch(
                    workflow, node_id, requested_targets
                )
                self._validate_unresolved_retry_targets(
                    workflow, node_id, requested_targets
                )
                warnings.extend(
                    self._shared_parameter_retry_warnings(
                        workflow, node_id, requested_targets
                    )
                )
            self._validate_active_reset_prerequisite(workflow, node_id, merged)
            self._validate_node_prerequisites(workflow, node_id, merged)
            if node_id == "03a":
                self._validate_03a_run_request(workflow, merged)
            if autonomy_lease_id is not None:
                self._validate_autonomy_scope(
                    autonomy_lease, node_id=node_id, targets=list(merged["qubits"])
                )
                self._assert_autonomy_attempt_quota(
                    autonomy_lease, node_id, list(merged["qubits"])
                )
        except AutonomyQuotaError:
            raise
        except AutonomyScopeError as exc:
            # Target-set / stage planning mistakes have not touched hardware.
            # Keep the lease usable so the agent can resubmit the unresolved
            # subgroup instead of converting a recoverable error into halt.
            if autonomy_lease_id is not None:
                self.db.event(
                    "autonomy_action_rejected",
                    client_id,
                    {
                        "lease_id": autonomy_lease_id,
                        "node_id": node_id,
                        "targets": sorted(requested_targets),
                        "reason": str(exc),
                    },
                    workflow_id,
                )
            raise
        script = self.policy.node_script(node_id)
        definition = self.policy.node_definition(node_id)
        payload = {
            "node_id": node_id,
            "node_name": definition["name"],
            "script_path": str(script),
            "parameters": merged,
            "reason": reason.strip(),
            "warnings": warnings,
            "conversational": conversational,
            "prerequisite_verification": prerequisite_verification,
        }
        payload["duration_prediction"] = self.run_telemetry(
            node_id, list(merged["qubits"]), limit=100
        )["prediction"]
        payload["prior_decision_experience"] = self.decision_experience(
            node_id, list(merged["qubits"]), limit=5
        )
        review_run_id = self._latest_review_run_id(workflow_id, node_id)
        if review_run_id is not None:
            payload["review_run_id"] = review_run_id
        proposal = self._create_proposal(
            workflow_id=workflow_id,
            kind="run",
            payload=payload,
            source_client=client_id,
            ttl_minutes=int(self.policy.raw["approval"]["run_ttl_minutes"]),
            max_uses=1 + auto_retry_count,
            autonomy_lease_id=autonomy_lease_id,
        )
        if autonomy_lease_id is not None:
            proposal = self._delegate_proposal_to_autonomy(
                proposal["id"], autonomy_lease_id, client_id
            )
        return proposal

    def request_conversational_run(
        self,
        workflow_id: str,
        node_id: str,
        parameters: dict[str, Any],
        reason: str,
        client_id: str,
        auto_retry_count: int = 0,
    ) -> dict[str, Any]:
        """Propose one policy-registered experiment outside automatic sequencing."""
        return self.request_run(
            workflow_id,
            node_id,
            parameters,
            reason,
            client_id,
            auto_retry_count,
            conversational=True,
        )

    def start_authorized_run(
        self,
        workflow_id: str,
        lease_id: str,
        node_id: str,
        parameters: dict[str, Any],
        reason: str,
        client_id: str,
    ) -> dict[str, Any]:
        """Validate, delegate, and launch one run under a human-approved lease."""
        try:
            proposal = self.request_run(
                workflow_id,
                node_id,
                parameters,
                reason,
                client_id,
                auto_retry_count=0,
                autonomy_lease_id=lease_id,
            )
            result = self.execute_run(proposal["id"], client_id)
            return {**result, "proposal_id": proposal["id"], "lease_id": lease_id}
        except AutonomyQuotaError:
            # Exhausting one node/qubit budget marks only that target incomplete.
            # The lease remains usable for other targets and downstream nodes.
            raise
        except AutonomyScopeError:
            # Recoverable target-set / stage planning error; lease stays active.
            raise
        except (PolicyError, RunnerError, ServiceError, ValueError) as exc:
            if self._is_recoverable_scheduling_block(exc):
                raise
            self._halt_autonomy(
                lease_id,
                f"Authorized run was refused or failed to launch: {exc}",
                "autonomy_guard",
                stop_active=True,
            )
            raise

    def execute_run(self, proposal_id: str, client_id: str) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        proposal = self.db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if proposal is None:
            raise ServiceError(f"Unknown proposal: {proposal_id}")
        if self._workflow(proposal["workflow_id"])["status"] != "active":
            raise ServiceError("Resume measurement mode before executing a run")
        lease_id = proposal.get("autonomy_lease_id")
        if lease_id:
            lease = self._active_autonomy(str(lease_id), proposal["workflow_id"])
            payload = json_loads(proposal["payload_json"], {})
            parameters = payload.get("parameters", {})
            targets = list(parameters.get("qubits", []))
            try:
                self._validate_autonomy_scope(
                    lease, node_id=str(payload.get("node_id", "")), targets=targets
                )
                self._assert_autonomy_attempt_quota(
                    lease, str(payload.get("node_id", "")), targets
                )
            except AutonomyQuotaError:
                raise
            except AutonomyScopeError as exc:
                # Same rule as `start_authorized_run`: the run is refused before
                # the worker launches, so the lease stays usable.
                self._record_refused_autonomy_action(
                    str(lease_id),
                    proposal["workflow_id"],
                    "autonomy_scope",
                    str(exc),
                )
                raise
        try:
            return self.runner.start_approved_run(proposal_id, client_id)
        except Exception as exc:
            if lease_id:
                self._halt_autonomy(
                    str(lease_id),
                    f"Worker launch or hardware lock failure: {exc}",
                    "autonomy_guard",
                    stop_active=True,
                )
            raise

    def poll_run(self, run_id: str) -> dict[str, Any]:
        run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if run is None:
            raise ServiceError(f"Unknown run: {run_id}")
        if run["status"] == "failed" and run["analysis_status"] == "not_started":
            failure = {
                "analysis_status": "failed",
                "node_id": run["node_id"],
                "snapshot_id": run.get("snapshot_id"),
                "snapshot_path": run.get("snapshot_path"),
                "plots": [],
                "failure_reasons": [
                    "Execution failed before a usable snapshot was produced."
                ],
                "warnings": [],
                "candidate_state_patch": [],
                "error": run.get("error"),
                "active_state_hash": run.get("active_state_hash_after"),
            }
            self.db.execute(
                "UPDATE runs SET analysis_status = 'failed', analysis_json = ? "
                "WHERE id = ? AND analysis_status = 'not_started'",
                (json_dumps(failure), run_id),
            )
            run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if self._is_instrument_connectivity_failure(run):
            self._pause_for_instrument_connectivity(run)
            run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if run.get("autonomy_lease_id"):
            self._enforce_run_hard_stops(run)
            run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        decoded = self._decode_run(run)
        if self._is_instrument_connectivity_failure(run):
            decoded["operator_handoff"] = instrument_operator_handoff()
        else:
            submission = self._program_submission_timeout(run)
            if submission is not None and submission.get("measurement_paused"):
                decoded["operator_handoff"] = (
                    submission_escalation_operator_handoff()
                )
        return decoded

    def stop_run(self, run_id: str, client_id: str) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        return self.runner.stop(run_id, client_id)

    def analyze_run(self, run_id: str) -> dict[str, Any]:
        run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if run is None:
            raise ServiceError(f"Unknown run: {run_id}")
        if run["status"] != "completed":
            raise ServiceError("Only a completed run can be analyzed")
        result = self.analyzer.analyze_run(run)
        self.db.execute(
            """
            UPDATE runs SET analysis_status = ?, analysis_json = ? WHERE id = ?
            """,
            (result["analysis_status"], json_dumps(result), run_id),
        )
        self.db.event(
            "run_analyzed",
            "deterministic_analyzer",
            {"run_id": run_id, "analysis_status": result["analysis_status"]},
            run["workflow_id"],
        )
        return result

    def analyze_authorized_run(
        self, run_id: str, lease_id: str, client_id: str
    ) -> dict[str, Any]:
        run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if run is None or run.get("autonomy_lease_id") != lease_id:
            raise ServiceError("Run is not associated with this autonomy lease")
        self._active_autonomy(lease_id, run["workflow_id"])
        if run["status"] != "completed":
            # Refuse a premature analyze before the watchdog runs. Enforcing
            # hardware-anomaly rules as a side effect of a timing mistake used
            # to end the lease over a call that did nothing.
            self._record_refused_autonomy_action(
                lease_id,
                run["workflow_id"],
                "analysis_precondition",
                f"Run {run_id} is {run['status']}, not completed",
            )
            raise ServiceError(
                f"Only a completed run can be analyzed; run {run_id} is "
                f"{run['status']}"
            )
        self._enforce_run_hard_stops(run)
        try:
            return self.analyze_run(run_id)
        except ServiceError as exc:
            # A precondition the agent got wrong -- most often analyzing a run
            # that has not reached a terminal state yet. Nothing was consumed,
            # so refuse it and let the agent wait and call again.
            self._record_refused_autonomy_action(
                lease_id, run["workflow_id"], "analysis_precondition", str(exc)
            )
            raise
        except Exception as exc:
            # An unreadable or unusable snapshot is the configured
            # `snapshot_missing` hard stop; anything else is an unknown
            # analyzer failure, which pauses for the operator.
            if isinstance(exc, AnalysisError):
                self._halt_autonomy(
                    lease_id,
                    f"Snapshot analysis could not read run {run_id}: {exc}",
                    client_id,
                    stop_active=False,
                )
            else:
                self._pause_autonomy_for_review(
                    run,
                    f"Snapshot analysis raised an unexpected exception for run "
                    f"{run_id}: {exc}",
                    "autonomy_paused_for_analysis_failure",
                    client_id,
                )
            raise

    def record_decision(
        self,
        workflow_id: str,
        run_id: str,
        decision: str,
        reason: str,
        next_node: str | None,
        new_parameters: dict[str, Any] | None,
        state_patch: list[dict[str, Any]] | None,
        client_id: str,
    ) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active":
            raise ServiceError("Resume measurement mode before recording a decision")
        run = self.db.one(
            "SELECT * FROM runs WHERE id = ? AND workflow_id = ?",
            (run_id, workflow_id),
        )
        if run is None:
            raise ServiceError("Run does not belong to this workflow")
        proposal = self.db.one(
            "SELECT payload_json FROM proposals WHERE id = ?",
            (run["proposal_id"],),
        )
        proposal_payload = json_loads(
            proposal["payload_json"] if proposal is not None else None, {}
        )
        conversational = bool(proposal_payload.get("conversational", False))
        prerequisite_verification = bool(
            proposal_payload.get("prerequisite_verification", False)
        )
        if (
            not conversational
            and not prerequisite_verification
            and run["node_id"] != workflow["current_node"]
        ):
            raise ServiceError(
                f"Run node {run['node_id']} does not match current workflow node "
                f"{workflow['current_node']}"
            )
        if self.db.one("SELECT id FROM decisions WHERE run_id = ?", (run_id,)):
            raise ServiceError("A decision has already been recorded for this run")
        if run["analysis_status"] not in {"pass", "needs_review", "failed"}:
            raise ServiceError("Analyze the run before recording a decision")
        allowed_decisions = {"advance", "repeat", "manual_review", "stop"}
        if decision not in allowed_decisions:
            raise ServiceError(f"Decision must be one of {sorted(allowed_decisions)}")
        if not reason.strip():
            raise ServiceError("Decision reason is required")

        current = workflow["current_node"]
        sequence = list(self.settings.workflow_sequence)
        resolved_next: str | None
        if conversational:
            if next_node is not None:
                self.policy.node_definition(next_node)
            resolved_next = next_node
        elif prerequisite_verification:
            if run["node_id"] not in self._active_reset_verification_nodes():
                raise ServiceError("Invalid active-reset prerequisite verification node")
            if not _is_active_reset(json_loads(run.get("parameters_json"), {})):
                raise ServiceError("Prerequisite verification must use active reset")
            if decision == "advance":
                rows = [
                    {
                        **run,
                        "decision": "advance",
                    }
                ]
                resolved = self._resolve_targets_from_rows(run["node_id"], rows)
                if not resolved:
                    raise ServiceError(
                        "Cannot accept active-reset verification without at least "
                        "one usable target result"
                    )
            if next_node not in {None, current}:
                raise ServiceError(
                    f"Prerequisite verification keeps the workflow at {current}"
                )
            resolved_next = None if decision == "stop" else current
        elif decision == "repeat":
            resolved_next = current
        elif decision == "advance":
            if current in SUBGROUP_NODES:
                resolved_targets = self._node_target_resolution(
                    workflow_id, current
                )
                eligible_targets = self._active_targets_for_node(workflow, current)
                incomplete_targets = self._autonomy_incomplete_targets_for_node(
                    workflow_id, current
                )
                missing_targets = (
                    eligible_targets - resolved_targets - incomplete_targets
                )
                if missing_targets:
                    raise ServiceError(
                        f"Cannot advance from {current} until every active workflow "
                        "target is resolved, is marked scientifically unmeasurable, "
                        "or reaches its autonomy attempt limit; "
                        f"missing {sorted(missing_targets)}"
                    )
                if not (eligible_targets & resolved_targets):
                    raise ServiceError(
                        f"Cannot advance from {current}: no target has usable "
                        "evidence for the next node"
                    )
                statistics_dependency = {
                    "05": "05st",
                    "06": "06st_t2star",
                    "06b": "06st_t2e",
                }.get(current)
                if statistics_dependency in sequence:
                    insufficient = {
                        target
                        for target in eligible_targets & resolved_targets
                        if not self._target_has_lifetime_coverage(
                            workflow_id, current, target, 3.5
                        )
                    }
                    if insufficient:
                        raise ServiceError(
                            f"Cannot advance from {current}: downstream 100-run "
                            "statistics require decay-to-equilibrium evidence covering "
                            f"at least 3.5 lifetimes for {sorted(insufficient)}"
                        )
            index = sequence.index(current)
            expected = sequence[index + 1] if index + 1 < len(sequence) else None
            if next_node != expected:
                raise ServiceError(f"Next node after {current} must be {expected}")
            resolved_next = expected
        elif decision == "manual_review":
            resolved_next = current
        else:
            resolved_next = None

        resolved_parameters = dict(new_parameters or {})
        parameter_adjustments: list[str] = []
        if resolved_next is not None and resolved_parameters:
            targets = (
                set(json_loads(workflow["targets_json"], []))
                if conversational
                else self._active_targets_for_node(workflow, resolved_next)
            )
            if (
                not conversational
                and not prerequisite_verification
                and decision == "advance"
                and current in SUBGROUP_NODES
            ):
                targets &= self._node_target_resolution(workflow_id, current)
            self.policy.validate_run(
                resolved_next,
                {"qubits": sorted(targets), **resolved_parameters},
            )
        patch = state_patch or []
        if patch:
            self.policy.validate_state_patch(patch)

        decision_id = uuid.uuid4().hex
        terminal_advance = (
            not conversational
            and not prerequisite_verification
            and decision == "advance"
            and resolved_next is None
        )
        workflow_status = "stopped" if decision == "stop" else (
            "completed" if terminal_advance and not patch else "active"
        )
        workflow_next = (
            current
            if conversational or (prerequisite_verification and decision != "stop")
            else resolved_next
        )
        try:
            with self.db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO decisions(
                        id, workflow_id, run_id, decision, reason, next_node,
                        next_parameters_json, state_patch_json, client_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        workflow_id,
                        run_id,
                        decision,
                        reason.strip(),
                        resolved_next,
                        json_dumps(resolved_parameters),
                        json_dumps(patch),
                        client_id,
                        utc_now(),
                    ),
                )
                connection.execute(
                    "UPDATE workflows SET current_node = ?, status = ?, "
                    "updated_at = ? WHERE id = ? AND status = 'active'",
                    (workflow_next, workflow_status, utc_now(), workflow_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ServiceError("A decision has already been recorded for this run") from exc
        if (
            not conversational
            and not prerequisite_verification
            and decision == "advance"
            and current in SUBGROUP_NODES
        ):
            incomplete_targets = self._autonomy_incomplete_targets_for_node(
                workflow_id, current
            )
            if incomplete_targets:
                self.db.event(
                    "autonomy_targets_incomplete",
                    client_id,
                    {
                        "node_id": current,
                        "targets": sorted(incomplete_targets),
                        "continued_targets": sorted(
                            self._node_target_resolution(workflow_id, current)
                        ),
                    },
                    workflow_id,
                )
        if workflow_status in {"completed", "stopped"}:
            lease_status = "completed" if workflow_status == "completed" else "revoked"
            self.db.execute(
                "UPDATE autonomy_leases SET status = ?, stopped_reason = ?, "
                "updated_at = ? WHERE workflow_id = ? "
                "AND status IN ('active', 'paused')",
                (
                    lease_status,
                    f"Workflow {workflow_status} by recorded decision.",
                    utc_now(),
                    workflow_id,
                ),
            )
        elif (
            not conversational
            and not prerequisite_verification
            and decision == "advance"
            and not patch
        ):
            self._complete_autonomy_scope_if_needed(
                workflow_id, resolved_next, client_id
            )
        result = {
            "decision_id": decision_id,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "Decision": decision,
            "Reason": reason.strip(),
            "next_action": {
                "next_node": resolved_next,
                "new_parameters": resolved_parameters,
            },
            "candidate_state_patch": patch,
            "state_commit_required": bool(patch),
            "parameter_adjustments": parameter_adjustments,
            "mode": (
                "conversational"
                if conversational
                else "prerequisite_verification"
                if prerequisite_verification
                else "sequenced"
            ),
        }
        self.db.event("decision_recorded", client_id, result, workflow_id)
        return result

    def request_state_commit(
        self,
        workflow_id: str,
        patch: list[dict[str, Any]],
        reason: str,
        client_id: str,
        run_id: str | None = None,
        autonomy_lease_id: str | None = None,
        _trusted_setup: bool = False,
    ) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        if not reason.strip():
            raise ServiceError("A state-commit reason is required")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active":
            raise ServiceError("Resume measurement mode before proposing a state update")
        autonomy_lease = (
            self._active_autonomy(autonomy_lease_id, workflow_id)
            if autonomy_lease_id is not None
            else None
        )
        try:
            self.policy.validate_state_patch(patch)
        except PolicyError as exc:
            # The patch is rejected before any proposal exists, so `state.json`
            # is untouched. Refuse and keep the lease.
            if autonomy_lease_id is not None:
                self._record_refused_autonomy_action(
                    autonomy_lease_id,
                    workflow_id,
                    "state_patch_policy_violation",
                    str(exc),
                )
            raise
        workflow_targets = set(json_loads(workflow["targets_json"], []))
        for item in patch:
            match = re.fullmatch(r"/qubits/(q[0-9]+)/.*", str(item.get("path", "")))
            if match is None or match.group(1) not in workflow_targets:
                raise ServiceError(
                    "Every state patch path must belong to a workflow target"
                )
        if run_id is not None:
            run = self.db.one(
                "SELECT * FROM runs WHERE id = ? AND workflow_id = ?",
                (run_id, workflow_id),
            )
            if run is None:
                raise ServiceError("State proposal run does not belong to this workflow")
            if run["analysis_status"] not in {"pass", "needs_review", "failed"}:
                raise ServiceError("Analyze the run before proposing its state update")
        if autonomy_lease_id is not None:
            try:
                if _trusted_setup:
                    self._validate_autonomy_patch_targets(autonomy_lease, patch)
                else:
                    self._validate_autonomous_scientific_state_commit(
                        autonomy_lease, run_id, patch
                    )
            except AutonomyScopeError as exc:
                # Out-of-scope commit, refused before the proposal is created.
                self._record_refused_autonomy_action(
                    autonomy_lease_id,
                    workflow_id,
                    "state_commit_scope",
                    str(exc),
                )
                raise
        payload = {
            "patch": patch,
            "reason": reason.strip(),
            "run_id": run_id,
            "base_hash": sha256_file(self.settings.active_state),
            "active_state": str(self.settings.active_state),
            "authorization_evidence": (
                "deterministic_setup"
                if _trusted_setup and autonomy_lease_id
                else "accepted_run"
                if autonomy_lease_id
                else "human_proposal"
            ),
        }
        proposal = self._create_proposal(
            workflow_id,
            "state_commit",
            payload,
            client_id,
            int(self.policy.raw["approval"]["state_commit_ttl_minutes"]),
            1,
            autonomy_lease_id=autonomy_lease_id,
        )
        if autonomy_lease_id is not None:
            proposal = self._delegate_proposal_to_autonomy(
                proposal["id"], autonomy_lease_id, client_id
            )
        return proposal

    def commit_authorized_state(
        self,
        workflow_id: str,
        lease_id: str,
        patch: list[dict[str, Any]],
        reason: str,
        client_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Create and atomically apply a pass-backed state patch under a lease."""
        try:
            proposal = self.request_state_commit(
                workflow_id,
                patch,
                reason,
                client_id,
                run_id,
                autonomy_lease_id=lease_id,
            )
            result = self.apply_state_commit(proposal["id"], client_id)
            return {**result, "lease_id": lease_id}
        except (AutonomyEvidenceError, AutonomyScopeError, PolicyError):
            # Refused before `state.json` was written; the inner layer has
            # already recorded it and the lease stays usable.
            raise
        except (StateError, ValueError) as exc:
            # A conflict while applying: the state file may no longer match what
            # the evidence was derived from. `apply_state_commit` halts on its
            # own for a claimed proposal; halt here for the rest.
            self._halt_autonomy(
                lease_id,
                f"Authorized state commit conflicted while applying: {exc}",
                "autonomy_guard",
                stop_active=True,
            )
            raise

    def apply_authorized_setup_state(
        self, proposal_id: str, lease_id: str, client_id: str
    ) -> dict[str, Any]:
        """Apply a trusted bootstrap/LO setup proposal delegated by the lease."""
        proposal = self.db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if (
            proposal is None
            or proposal.get("autonomy_lease_id") != lease_id
            or proposal["kind"] != "state_commit"
        ):
            raise AutonomyScopeError(
                "Setup proposal is not bound to this autonomy lease"
            )
        payload = json_loads(proposal["payload_json"], {})
        if payload.get("authorization_evidence") != "deterministic_setup":
            raise AutonomyEvidenceError(
                "Only a server-generated deterministic setup may use this tool"
            )
        self._active_autonomy(lease_id, proposal["workflow_id"])
        try:
            result = self.apply_state_commit(proposal_id, client_id)
            return {**result, "lease_id": lease_id}
        except (PolicyError, AutonomyScopeError, AutonomyEvidenceError) as exc:
            self._record_refused_autonomy_action(
                lease_id,
                proposal["workflow_id"],
                "deterministic_setup_refused",
                str(exc),
            )
            raise
        except Exception as exc:
            # Anything else here means the write itself failed or conflicted.
            self._halt_autonomy(
                lease_id,
                f"Authorized setup commit failed: {exc}",
                "autonomy_guard",
                stop_active=True,
            )
            raise

    def request_bootstrap(
        self,
        workflow_id: str,
        qubits: list[str],
        client_id: str,
        autonomy_lease_id: str | None = None,
    ) -> dict[str, Any]:
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active":
            raise ServiceError("Resume measurement mode before bootstrap")
        targets = set(json_loads(workflow["targets_json"], []))
        if not qubits or not set(qubits).issubset(targets):
            raise ServiceError("Bootstrap qubits must be workflow targets")
        state = load_state(self.settings.active_state)
        patch, details = bootstrap_patch(state, qubits, self.policy.raw)
        if not patch:
            return {
                "proposal_created": False,
                "message": "All selected x180/x90 amplitudes are already nonzero.",
                "details": details,
            }
        proposal = self.request_state_commit(
            workflow_id,
            patch,
            "Bootstrap missing/zero x180 at 50% of max_x180_wf_amplitude; "
            "set missing/zero x90 from x180/2.",
            client_id,
            autonomy_lease_id=autonomy_lease_id,
            _trusted_setup=bool(autonomy_lease_id),
        )
        proposal["bootstrap_details"] = details
        return proposal

    def request_07b_prerequisites(
        self,
        workflow_id: str,
        qubits: list[str],
        client_id: str,
        autonomy_lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Initialize missing IQ-blob output keys before Qualibrate records them.

        Qualibrate's state-update recorder must read the old mapping value before
        07b assigns a measured fidelity. A zero sentinel is safe because it cannot
        pass 07b analysis and a successful run replaces it with measured evidence.
        """
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active" or workflow["current_node"] != "07b":
            raise ServiceError("07b prerequisites require an active 07b workflow")
        active_targets = self._active_targets_for_node(workflow, "07b")
        selected = set(qubits)
        if (
            not selected
            or len(selected) != len(qubits)
            or not selected.issubset(active_targets)
        ):
            raise ServiceError(
                "07b prerequisite qubits must be unique active workflow targets"
            )

        state = load_state(self.settings.active_state)
        patch: list[dict[str, Any]] = []
        details: dict[str, dict[str, Any]] = {}
        for name in qubits:
            qubit = state.get("qubits", {}).get(name)
            if not isinstance(qubit, dict):
                raise ServiceError(f"Active state has no qubit {name}")
            extras = qubit.get("extras")
            if extras is None:
                extras = {}
            if not isinstance(extras, dict):
                raise ServiceError(f"{name} extras must be a mapping")
            already_present = "readout_fidelity" in extras
            details[name] = {
                "readout_fidelity_already_present": already_present,
                "placeholder": None if already_present else 0.0,
            }
            if not already_present:
                patch.append(
                    {
                        "op": "add",
                        "path": f"/qubits/{name}/extras/readout_fidelity",
                        "value": 0.0,
                    }
                )

        if not patch:
            return {
                "proposal_created": False,
                "message": "All selected 07b readout-fidelity keys already exist.",
                "07b_prerequisite_details": details,
            }
        proposal = self.request_state_commit(
            workflow_id,
            patch,
            "Initialize only missing readout_fidelity mapping keys to a non-passing "
            "zero sentinel so 07b can record and later replace measured values.",
            client_id,
            autonomy_lease_id=autonomy_lease_id,
            _trusted_setup=bool(autonomy_lease_id),
        )
        proposal["07b_prerequisite_details"] = details
        return proposal

    def request_07b_tail_power_reduction(
        self,
        workflow_id: str,
        qubits: list[str],
        evidence_run_id: str,
        client_id: str,
        autonomy_lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Reduce readout amplitude one fixed step after long-tail evidence."""
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active" or workflow["current_node"] != "07b":
            raise ServiceError("07b power reduction requires an active 07b workflow")
        active_targets = self._active_targets_for_node(workflow, "07b")
        selected = set(qubits)
        if (
            not selected
            or len(selected) != len(qubits)
            or not selected.issubset(active_targets)
        ):
            raise ServiceError("Power-reduction qubits must be unique active targets")
        evidence = self.db.one(
            "SELECT * FROM runs WHERE id = ? AND workflow_id = ? AND node_id = '07b'",
            (evidence_run_id, workflow_id),
        )
        if (
            evidence is None
            or evidence["status"] != "completed"
            or evidence["analysis_status"] == "pass"
        ):
            raise ServiceError("Power reduction requires non-passing saved 07b evidence")
        analysis = json_loads(evidence.get("analysis_json"), {})
        metrics = analysis.get("dataset_metrics", {}).get("qubits", {})
        unsupported = [
            name
            for name in qubits
            if not isinstance(metrics.get(name), dict)
            or metrics[name].get("morphology_pass") is not False
        ]
        if unsupported:
            raise ServiceError(
                "Selected targets lack failed long-tail morphology evidence: "
                f"{sorted(unsupported)}"
            )

        rules = self.policy.raw["analysis"]["07b"]
        factor = float(rules["tail_retry_amplitude_factor"])
        minimum = float(rules["min_readout_amplitude"])
        state = load_state(self.settings.active_state)
        patch: list[dict[str, Any]] = []
        details: dict[str, dict[str, float]] = {}
        for name in qubits:
            readout = (
                state.get("qubits", {})
                .get(name, {})
                .get("resonator", {})
                .get("operations", {})
                .get("readout")
            )
            if not isinstance(readout, dict):
                raise ServiceError(f"{name} readout operation must be an inline mapping")
            amplitude = readout.get("amplitude")
            if (
                not isinstance(amplitude, (int, float))
                or isinstance(amplitude, bool)
                or not math.isfinite(float(amplitude))
                or float(amplitude) <= minimum
            ):
                raise ServiceError(
                    f"{name} readout amplitude cannot be reduced safely below {minimum:g}"
                )
            reduced = max(minimum, float(amplitude) * factor)
            patch.append(
                {
                    "op": "replace",
                    "path": f"/qubits/{name}/resonator/operations/readout/amplitude",
                    "value": reduced,
                }
            )
            details[name] = {
                "old_amplitude": float(amplitude),
                "new_amplitude": reduced,
                "amplitude_factor": factor,
                "power_change_db": 20.0 * math.log10(factor),
            }
        proposal = self.request_state_commit(
            workflow_id,
            patch,
            "Reduce readout amplitude by the fixed 07b tail-recovery step after "
            f"snapshot {evidence_run_id} showed elongated/tail morphology.",
            client_id,
            autonomy_lease_id=autonomy_lease_id,
            _trusted_setup=bool(autonomy_lease_id),
        )
        proposal["07b_tail_power_reduction"] = {
            "evidence_run_id": evidence_run_id,
            "targets": details,
        }
        return proposal

    def request_drive_lo_recenter(
        self,
        workflow_id: str,
        qubits: list[str],
        client_id: str,
        autonomy_lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Propose preserving drive RF while moving private XY LOs to a grid."""
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active":
            raise ServiceError("Resume measurement mode before LO recentering")
        if workflow["current_node"] != "03a":
            raise ServiceError("Drive LO recentering is available only at node 03a")
        targets = self._active_targets_for_node(workflow, "03a")
        if not qubits or not set(qubits).issubset(targets):
            raise ServiceError("LO recenter qubits must be active workflow targets")

        state = load_state(self.settings.active_state)
        wiring = load_state(self.settings.wiring_path)
        try:
            lo_grid_hz = float(
                self.policy.raw["instrument_limits"]["drive_lo_grid_hz"]
            )
            patch, details = drive_lo_recenter_patch(
                state, wiring, qubits, lo_grid_hz
            )
            self.policy.validate_state_patch(patch, state)
        except (PolicyError, ValueError) as exc:
            if autonomy_lease_id:
                # Refused before the setup proposal exists; nothing was
                # written, so the lease stays usable.
                self._record_refused_autonomy_action(
                    autonomy_lease_id,
                    workflow_id,
                    "deterministic_setup_policy_violation",
                    f"LO recenter setup: {exc}",
                )
            raise ServiceError(str(exc)) from exc
        payload = {
            "patch": patch,
            "reason": (
                "Preserve each selected qubit's current drive RF frequency while "
                "moving its private XY LO to the nearest 100 MHz grid point and "
                "keeping only the residual IF before a wide 03a spectroscopy sweep."
            ),
            "run_id": None,
            "base_hash": sha256_file(self.settings.active_state),
            "active_state": str(self.settings.active_state),
            "drive_lo_recenter": details,
            "authorization_evidence": (
                "deterministic_setup" if autonomy_lease_id else "human_proposal"
            ),
        }
        if autonomy_lease_id:
            lease = self._active_autonomy(autonomy_lease_id, workflow_id)
            self._validate_autonomy_scope(lease, node_id="03a", targets=qubits)
        proposal = self._create_proposal(
            workflow_id,
            "state_commit",
            payload,
            client_id,
            int(self.policy.raw["approval"]["state_commit_ttl_minutes"]),
            1,
            autonomy_lease_id=autonomy_lease_id,
        )
        if autonomy_lease_id:
            proposal = self._delegate_proposal_to_autonomy(
                proposal["id"], autonomy_lease_id, client_id
            )
        proposal["drive_lo_recenter"] = details
        return proposal

    def request_initial_03a_zero_if(
        self,
        workflow_id: str,
        client_id: str,
        autonomy_lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Propose IF=0 for every active target before the first 03a coarse run."""
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active" or workflow["current_node"] != "03a":
            raise ServiceError("Initial 03a LO setup requires an active 03a workflow")
        prior = self.db.one(
            "SELECT id FROM runs WHERE workflow_id = ? AND node_id = '03a' "
            "AND status = 'completed' LIMIT 1",
            (workflow_id,),
        )
        if prior is not None:
            raise ServiceError(
                "Initial 03a LO setup is available only before its first run"
            )
        active = self._active_targets_for_node(workflow, "03a")
        qubits = [
            name
            for name in json_loads(workflow["targets_json"], [])
            if name in active
        ]
        state = load_state(self.settings.active_state)
        wiring = load_state(self.settings.wiring_path)
        try:
            lo_grid_hz = float(
                self.policy.raw["instrument_limits"]["drive_lo_grid_hz"]
            )
            patch, details = drive_lo_recenter_patch(
                state,
                wiring,
                qubits,
                lo_grid_hz,
                force_zero_if=True,
            )
            self.policy.validate_state_patch(patch, state)
        except (PolicyError, ValueError) as exc:
            if autonomy_lease_id:
                # Refused before the setup proposal exists; nothing was
                # written, so the lease stays usable.
                self._record_refused_autonomy_action(
                    autonomy_lease_id,
                    workflow_id,
                    "deterministic_setup_policy_violation",
                    f"initial 03a setup: {exc}",
                )
            raise ServiceError(str(exc)) from exc
        payload = {
            "patch": patch,
            "reason": (
                "Before the first multiplexed 03a coarse search, preserve every "
                "active target's current RF by moving its private LO to that RF "
                "and setting every XY IF to zero, enabling an 800 MHz full span."
            ),
            "run_id": None,
            "base_hash": sha256_file(self.settings.active_state),
            "active_state": str(self.settings.active_state),
            "initial_03a_zero_if": details,
            "authorization_evidence": (
                "deterministic_setup" if autonomy_lease_id else "human_proposal"
            ),
        }
        if autonomy_lease_id:
            lease = self._active_autonomy(autonomy_lease_id, workflow_id)
            self._validate_autonomy_scope(lease, node_id="03a", targets=qubits)
        proposal = self._create_proposal(
            workflow_id,
            "state_commit",
            payload,
            client_id,
            int(self.policy.raw["approval"]["state_commit_ttl_minutes"]),
            1,
            autonomy_lease_id=autonomy_lease_id,
        )
        if autonomy_lease_id:
            proposal = self._delegate_proposal_to_autonomy(
                proposal["id"], autonomy_lease_id, client_id
            )
        proposal["initial_03a_zero_if"] = details
        return proposal

    def request_03a_window_shift(
        self,
        workflow_id: str,
        lo_centers_in_ghz: dict[str, float],
        client_id: str,
        autonomy_lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Propose 100 MHz-grid, IF=0 coarse windows for unresolved targets."""
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active" or workflow["current_node"] != "03a":
            raise ServiceError("03a window shifting requires an active 03a workflow")
        shift_ready = self._03a_shift_ready_targets(workflow_id)
        selected = set(lo_centers_in_ghz)
        if not selected or not selected.issubset(shift_ready):
            raise ServiceError(
                "Window-shift qubits must either be edge-limited or remain "
                "unresolved after 2000 averages; eligible targets are "
                f"{sorted(shift_ready)}"
            )
        centers_hz = {
            name: float(value) * 1e9
            for name, value in lo_centers_in_ghz.items()
        }
        state = load_state(self.settings.active_state)
        wiring = load_state(self.settings.wiring_path)
        try:
            lo_grid_hz = float(
                self.policy.raw["instrument_limits"]["drive_lo_grid_hz"]
            )
            patch, details = drive_lo_recenter_patch(
                state,
                wiring,
                list(lo_centers_in_ghz),
                lo_grid_hz,
                target_lo_hz=centers_hz,
            )
            self.policy.validate_state_patch(patch, state)
        except (PolicyError, ValueError) as exc:
            if autonomy_lease_id:
                # Refused before the setup proposal exists; nothing was
                # written, so the lease stays usable.
                self._record_refused_autonomy_action(
                    autonomy_lease_id,
                    workflow_id,
                    "deterministic_setup_policy_violation",
                    f"03a window shift: {exc}",
                )
            raise ServiceError(str(exc)) from exc
        payload = {
            "patch": patch,
            "reason": (
                "Move only unresolved 03a targets to explicitly selected "
                "100 MHz-grid LO coarse-search windows with IF=0."
            ),
            "run_id": None,
            "base_hash": sha256_file(self.settings.active_state),
            "active_state": str(self.settings.active_state),
            "03a_window_shift": details,
            "authorization_evidence": (
                "deterministic_setup" if autonomy_lease_id else "human_proposal"
            ),
        }
        if autonomy_lease_id:
            lease = self._active_autonomy(autonomy_lease_id, workflow_id)
            self._validate_autonomy_scope(
                lease, node_id="03a", targets=list(lo_centers_in_ghz)
            )
        proposal = self._create_proposal(
            workflow_id,
            "state_commit",
            payload,
            client_id,
            int(self.policy.raw["approval"]["state_commit_ttl_minutes"]),
            1,
            autonomy_lease_id=autonomy_lease_id,
        )
        if autonomy_lease_id:
            proposal = self._delegate_proposal_to_autonomy(
                proposal["id"], autonomy_lease_id, client_id
            )
        proposal["03a_window_shift"] = details
        return proposal

    def request_03a_candidate_center(
        self,
        workflow_id: str,
        qubits: list[str],
        client_id: str,
        autonomy_lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Place credible unresolved 03a candidates at safe LO/IF centers."""
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        workflow = self._workflow(workflow_id)
        if workflow["status"] != "active" or workflow["current_node"] != "03a":
            raise ServiceError("03a candidate centering requires an active 03a workflow")
        selected = set(qubits)
        eligible = (
            self._03a_candidate_targets(workflow_id)
            - self._node_target_resolution(workflow_id, "03a")
        )
        if not selected or len(selected) != len(qubits) or not selected.issubset(eligible):
            raise ServiceError(
                "Candidate-center qubits must be credible unresolved 03a targets; "
                f"eligible targets are {sorted(eligible)}"
            )

        rows = self.db.all(
            "SELECT r.id, r.status, r.analysis_status, r.analysis_json, "
            "r.parameters_json, d.decision FROM runs r "
            "LEFT JOIN decisions d ON d.run_id = r.id "
            "WHERE r.workflow_id = ? AND r.node_id = '03a' ORDER BY r.rowid",
            (workflow_id,),
        )
        min_snr = float(self.policy.raw["analysis"]["03a"]["min_robust_snr"])
        target_rf_hz: dict[str, float] = {}
        evidence: dict[str, dict[str, Any]] = {}
        for name in qubits:
            for row in reversed(rows):
                if name not in resolve_03a_candidate_targets([row], min_snr):
                    continue
                analysis = json_loads(row.get("analysis_json"), {})
                fit = analysis.get("fit_quality", {}).get("results", {}).get(name, {})
                drive_freq = fit.get("drive_freq") if isinstance(fit, dict) else None
                if (
                    isinstance(drive_freq, (int, float))
                    and not isinstance(drive_freq, bool)
                    and math.isfinite(float(drive_freq))
                ):
                    target_rf_hz[name] = float(drive_freq)
                    metrics = analysis.get("dataset_metrics", {}).get("qubits", {}).get(name, {})
                    evidence[name] = {
                        "run_id": row["id"],
                        "candidate_rf_hz": float(drive_freq),
                        "robust_snr": metrics.get("robust_snr") if isinstance(metrics, dict) else None,
                        "edge_fraction": metrics.get("edge_fraction") if isinstance(metrics, dict) else None,
                        "03a_stage": analysis.get("03a_stage"),
                    }
                    break
            if name not in target_rf_hz:
                raise ServiceError(f"No finite credible 03a candidate frequency for {name}")

        state = load_state(self.settings.active_state)
        wiring = load_state(self.settings.wiring_path)
        try:
            lo_grid_hz = float(
                self.policy.raw["instrument_limits"]["drive_lo_grid_hz"]
            )
            patch, details = drive_lo_recenter_patch(
                state,
                wiring,
                qubits,
                lo_grid_hz,
                target_rf_hz=target_rf_hz,
            )
            self.policy.validate_state_patch(patch, state)
        except (PolicyError, ValueError) as exc:
            if autonomy_lease_id:
                # Refused before the setup proposal exists; nothing was
                # written, so the lease stays usable.
                self._record_refused_autonomy_action(
                    autonomy_lease_id,
                    workflow_id,
                    "deterministic_setup_policy_violation",
                    f"03a candidate center: {exc}",
                )
            raise ServiceError(str(exc)) from exc
        for name, item in details.items():
            item["candidate_evidence"] = evidence[name]

        payload = {
            "patch": patch,
            "reason": (
                "Center each credible unresolved 03a candidate using the nearest "
                "100 MHz-grid private XY LO and the residual IF, preserving the "
                "evidence-backed candidate RF before a low-power fine scan."
            ),
            "run_id": None,
            "base_hash": sha256_file(self.settings.active_state),
            "active_state": str(self.settings.active_state),
            "03a_candidate_center": details,
            "authorization_evidence": (
                "deterministic_setup" if autonomy_lease_id else "human_proposal"
            ),
        }
        if autonomy_lease_id:
            lease = self._active_autonomy(autonomy_lease_id, workflow_id)
            self._validate_autonomy_scope(lease, node_id="03a", targets=qubits)
        proposal = self._create_proposal(
            workflow_id,
            "state_commit",
            payload,
            client_id,
            int(self.policy.raw["approval"]["state_commit_ttl_minutes"]),
            1,
            autonomy_lease_id=autonomy_lease_id,
        )
        if autonomy_lease_id:
            proposal = self._delegate_proposal_to_autonomy(
                proposal["id"], autonomy_lease_id, client_id
            )
        proposal["03a_candidate_center"] = details
        return proposal

    def apply_state_commit(self, proposal_id: str, client_id: str) -> dict[str, Any]:
        if not client_id.strip():
            raise ServiceError("client_id is required for the audit log")
        proposal = self._approved_proposal(proposal_id, "state_commit")
        if self._workflow(proposal["workflow_id"])["status"] != "active":
            raise ServiceError("Resume measurement mode before applying a state update")
        payload = json_loads(proposal["payload_json"], {})
        lease_id = proposal.get("autonomy_lease_id")
        if lease_id:
            lease = self._active_autonomy(str(lease_id), proposal["workflow_id"])
            self._validate_autonomy_patch_targets(lease, payload["patch"])
        self.policy.validate_state_patch(payload["patch"])
        claimed = False
        try:
            with exclusive_file_lock(
                self.settings.runtime / "hardware-transition.lock",
                "state commit hardware exclusion",
            ):
                self._assert_state_commit_idle()
                changed = self.db.execute(
                    "UPDATE proposals SET status = 'applying' WHERE id = ? "
                    "AND status = 'approved' AND uses < max_uses",
                    (proposal_id,),
                )
                if changed != 1:
                    raise ServiceError(
                        "State proposal was consumed or claimed concurrently"
                    )
                claimed = True
                result = commit_state(
                    self.settings.active_state,
                    payload["patch"],
                    self.settings.runtime / "backups",
                    payload["base_hash"],
                )
                self.db.execute(
                    "UPDATE proposals SET uses = uses + 1, status = 'consumed' "
                    "WHERE id = ? AND status = 'applying'",
                    (proposal_id,),
                )
        except Exception as exc:
            if claimed:
                self.db.execute(
                    "UPDATE proposals SET status = 'approved' WHERE id = ? "
                    "AND status = 'applying'",
                    (proposal_id,),
                )
            if lease_id:
                self._halt_autonomy(
                    str(lease_id),
                    f"State hash conflict or commit failure: {exc}",
                    "autonomy_guard",
                    stop_active=True,
                )
            raise
        output = {
            "proposal_id": proposal_id,
            "status": "committed",
            **result,
            "patch": payload["patch"],
        }
        self.db.event(
            "state_committed", client_id, output, proposal["workflow_id"]
        )
        run_id = payload.get("run_id")
        if run_id:
            terminal = self.db.one(
                "SELECT id FROM decisions WHERE run_id = ? AND decision = 'advance' "
                "AND next_node IS NULL ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            )
            if terminal is not None:
                self.db.execute(
                    "UPDATE workflows SET status = 'completed', updated_at = ? "
                    "WHERE id = ? AND current_node IS NULL",
                    (utc_now(), proposal["workflow_id"]),
                )
                self.db.execute(
                    "UPDATE autonomy_leases SET status = 'completed', "
                    "stopped_reason = ?, updated_at = ? WHERE workflow_id = ? "
                    "AND status IN ('active', 'paused')",
                    (
                        "Workflow completed after its terminal state commit.",
                        utc_now(),
                        proposal["workflow_id"],
                    ),
                )
            else:
                workflow = self._workflow(proposal["workflow_id"])
                self._complete_autonomy_scope_if_needed(
                    proposal["workflow_id"], workflow.get("current_node"), client_id
                )
        return output

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if proposal is None:
            raise ServiceError(f"Unknown proposal: {proposal_id}")
        return self._decode_proposal(proposal)

    def approve_interactively(self, proposal_id: str) -> dict[str, Any]:
        return self._approve_pending_proposal(
            proposal_id,
            getpass.getuser(),
            "interactive_terminal",
        )

    @property
    def approval_local_origin(self) -> str:
        runtime = self._runtime_approval_configuration()
        host = str(runtime["host"])
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{runtime['port']}"

    @property
    def browser_approval_origin(self) -> str:
        runtime = self._runtime_approval_configuration()
        return str(runtime["public_base_url"] or self.approval_local_origin)

    @property
    def approval_transport(self) -> str:
        return str(self._runtime_approval_configuration()["transport"])

    @property
    def remote_approval_enabled(self) -> bool:
        runtime = self._runtime_approval_configuration()
        return bool(
            runtime["transport"] == "public"
            and runtime["public_base_url"]
            and runtime.get("access_secret_present")
            and runtime.get("password_present")
        )

    @property
    def approval_allowed_origins(self) -> tuple[str, ...]:
        runtime = self._runtime_approval_configuration()
        origins = {
            self.approval_local_origin,
            *self.settings.approval_trusted_origins,
        }
        if runtime["public_base_url"]:
            origins.add(str(runtime["public_base_url"]))
        return tuple(sorted(origins))

    def _runtime_approval_configuration(self) -> dict[str, Any]:
        """Resolve current Dashboard metadata without coupling STDIO startup to it."""
        fallback: dict[str, Any] = {
            "transport": self.settings.approval_transport,
            "host": self.settings.approval_host,
            "port": self.settings.approval_port,
            "public_base_url": self.settings.approval_public_base_url,
            "access_secret_present": bool(self.settings.approval_access_token),
            "password_present": self._dashboard_password_present(
                self.settings.runtime / "dashboard-password.txt"
            ),
        }
        bootstrap_path = self.settings.runtime / "server-bootstrap.json"
        try:
            raw = json.loads(bootstrap_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return fallback
        if not isinstance(raw, dict) or raw.get("status") == "stopped":
            return fallback

        host = str(raw.get("approval_bind_host") or "127.0.0.1").strip()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return fallback
        try:
            port = int(raw.get("approval_port") or self.settings.approval_port)
        except (TypeError, ValueError):
            return fallback
        if not 1 <= port <= 65535:
            return fallback

        provider = str(raw.get("remote_provider") or "local").casefold()
        if provider != "public":
            return {
                "transport": "local",
                "host": host,
                "port": port,
                "public_base_url": None,
                "access_secret_present": False,
                "password_present": False,
            }
        public_base_url = str(raw.get("public_base_url") or "").strip().rstrip("/")
        parsed = urlsplit(public_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return fallback

        secret_path_value = str(raw.get("approval_access_token_path") or "").strip()
        if not secret_path_value:
            return fallback
        try:
            secret_path = Path(secret_path_value).expanduser().resolve()
            runtime = self.settings.runtime.resolve()
            if secret_path != runtime and runtime not in secret_path.parents:
                return fallback
            secret = secret_path.read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            return fallback
        if len(secret) < 64:
            return fallback
        # The long-lived secret authenticates service health only.
        # It must never be returned in a browser URL.
        password_path_value = str(raw.get("dashboard_password_path") or "").strip()
        if not password_path_value:
            return fallback
        try:
            password_path = Path(password_path_value).expanduser().resolve()
            if password_path != runtime and runtime not in password_path.parents:
                return fallback
        except (OSError, ValueError):
            return fallback
        return {
            "transport": "public",
            "host": host,
            "port": port,
            "public_base_url": public_base_url,
            "access_secret_present": True,
            "password_present": self._dashboard_password_present(password_path),
        }

    @staticmethod
    def _dashboard_password_present(path: Path) -> bool:
        try:
            password = path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            return False
        return 12 <= len(password) <= 256 and "\n" not in password and "\r" not in password

    def browser_approval_url(self, proposal_id: str) -> str:
        return self._external_browser_url(f"/approve/{proposal_id}")

    def session_dashboard_url(self, session_id: str) -> str:
        """Return the single public entry URL shared by phones and computers."""
        return self._external_browser_url("/")

    def session_home_url(self, session_id: str) -> str:
        return self._external_browser_url(f"/session/{session_id}/home")

    def session_approval_url(self, session_id: str) -> str:
        return self._external_browser_url(f"/session/{session_id}/approval")

    def session_results_url(self, session_id: str) -> str:
        return self._external_browser_url(f"/session/{session_id}/results")

    def current_dashboard_session_id(self) -> str | None:
        row = self.db.one(
            "SELECT id FROM measurement_sessions "
            "WHERE status IN ('active','paused','halted','stopping',"
            "'recovery_required') ORDER BY updated_at DESC LIMIT 1"
        )
        return str(row["id"]) if row is not None else None

    def dashboard_session_id_for_workflow(self, workflow_id: str) -> str | None:
        row = self.db.one(
            "SELECT id FROM measurement_sessions WHERE workflow_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (workflow_id,),
        )
        return str(row["id"]) if row is not None else None

    @property
    def operator_console_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.port}/operator"

    def _external_browser_url(self, path: str) -> str:
        return f"{self.browser_approval_origin}{path}"

    def _start_dashboard_session(
        self,
        workflow_id: str,
        mode: str,
        source_client: str,
        *,
        autonomy_lease_id: str | None = None,
    ) -> dict[str, Any]:
        if mode not in {"conversational", "autonomous"}:
            raise ServiceError("Unknown dashboard mode")
        existing = self.db.one(
            "SELECT * FROM measurement_sessions WHERE workflow_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (workflow_id,),
        )
        created = False
        if existing is None:
            now = utc_now()
            session_id = uuid.uuid4().hex
            created = self.db.execute(
                "INSERT OR IGNORE INTO measurement_sessions("
                "id, workflow_id, mode, status, started_at, updated_at, "
                "source_client, autonomy_lease_id) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
                (
                    session_id,
                    workflow_id,
                    mode,
                    now,
                    now,
                    source_client,
                    autonomy_lease_id,
                ),
            ) == 1
            existing = self.db.one(
                "SELECT * FROM measurement_sessions WHERE workflow_id = ?",
                (workflow_id,),
            )
            if created:
                self.db.event(
                    "dashboard_session_started",
                    source_client,
                    {
                        "session_id": session_id,
                        "mode": mode,
                        "autonomy_lease_id": autonomy_lease_id,
                    },
                    workflow_id,
                )
        if existing is None:
            raise ServiceError("Dashboard session could not be created")
        if not created:
            self.db.execute(
                "UPDATE measurement_sessions SET mode = ?, status = 'active', "
                "updated_at = ?, source_client = ?, autonomy_lease_id = ? "
                "WHERE id = ?",
                (
                    mode,
                    utc_now(),
                    source_client,
                    autonomy_lease_id,
                    existing["id"],
                ),
            )
            existing = self.db.one(
                "SELECT * FROM measurement_sessions WHERE id = ?", (existing["id"],)
            )
        return self._decode_dashboard_session(existing)

    def dashboard_status(self, session_id: str) -> dict[str, Any]:
        row = self.db.one(
            "SELECT * FROM measurement_sessions WHERE id = ?", (session_id,)
        )
        return self._decode_dashboard_session(row)

    def _decode_dashboard_session(
        self, row: dict[str, Any] | None
    ) -> dict[str, Any]:
        if row is None:
            raise ServiceError("Unknown measurement dashboard session")
        result = dict(row)
        result["browser_url"] = self.session_dashboard_url(str(result["id"]))
        result["home_url"] = self.session_home_url(str(result["id"]))
        result["approval_url"] = self.session_approval_url(str(result["id"]))
        result["results_url"] = self.session_results_url(str(result["id"]))
        result["workflow"] = self._decode_workflow(
            self._workflow(str(result["workflow_id"]))
        )
        lease_id = result.get("autonomy_lease_id")
        result["authorization"] = (
            self.autonomy_status(lease_id=str(lease_id)) if lease_id else None
        )
        result["operator_console_url"] = self.operator_console_url
        result["shutdown"] = self._shutdown_request_for_session(str(result["id"]))
        return result

    def dashboard_review(self, session_id: str) -> dict[str, Any]:
        raw_session = self.db.one(
            "SELECT workflow_id FROM measurement_sessions WHERE id = ?",
            (session_id,),
        )
        if raw_session is None:
            raise ServiceError("Unknown measurement dashboard session")
        failed_runs = self.db.all(
            "SELECT * FROM runs WHERE workflow_id = ? AND status = 'failed' "
            "ORDER BY started_at DESC LIMIT 20",
            (raw_session["workflow_id"],),
        )
        for failed_run in failed_runs:
            if self._is_instrument_connectivity_failure(failed_run):
                self._pause_for_instrument_connectivity(failed_run)
                break
        session = self.dashboard_status(session_id)
        workflow_id = str(session["workflow_id"])
        now = utc_now()
        self.db.execute(
            "UPDATE proposals SET status = 'expired' WHERE workflow_id = ? "
            "AND status = 'pending' AND expires_at <= ?",
            (workflow_id, now),
        )
        run_rows = self.db.all(
            "SELECT id FROM runs WHERE workflow_id = ? ORDER BY rowid",
            (workflow_id,),
        )
        pending = None
        autonomy_lease_id = session.get("autonomy_lease_id")
        if autonomy_lease_id:
            # The initial autonomy proposal is created immediately before its
            # stable Dashboard session is bound.  Find that proposal through
            # the session's lease instead of excluding it with started_at.
            pending = self.db.one(
                "SELECT p.id FROM proposals p "
                "JOIN autonomy_leases a ON a.proposal_id = p.id "
                "WHERE a.id = ? AND a.workflow_id = ? "
                "AND p.workflow_id = ? AND p.status = 'pending'",
                (autonomy_lease_id, workflow_id, workflow_id),
            )
        if pending is None:
            pending = self.db.one(
                "SELECT id FROM proposals WHERE workflow_id = ? "
                "AND created_at >= ? AND status = 'pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (workflow_id, session["started_at"]),
            )
        history: list[dict[str, Any]] = []
        all_plots: list[str] = []
        for row in run_rows:
            review = self._run_review(str(row["id"]), workflow_id)
            run_info = review.get("run") or {}
            parameters = run_info.get("parameters", {})
            review["duration_prediction"] = self.run_telemetry(
                str(run_info.get("node_id")),
                list(parameters.get("qubits", [])),
                limit=100,
            )["prediction"]
            item_plots = list(review.get("plots", []))
            review["asset_indices"] = list(
                range(len(all_plots), len(all_plots) + len(item_plots))
            )
            all_plots.extend(item_plots)
            history.append(review)
        return {
            "session": session,
            "pending_proposal": (
                self.proposal(str(pending["id"])) if pending is not None else None
            ),
            "history": history,
            "plots": all_plots,
            "event_revision": self.dashboard_ui_revision(session_id),
        }

    def dashboard_review_asset(self, session_id: str, index: int) -> Path:
        plots = self.dashboard_review(session_id)["plots"]
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(plots)
        ):
            raise ServiceError("Unknown dashboard review asset")
        path = Path(plots[index]).resolve()
        if not self._is_allowed_review_asset(path) or not path.is_file():
            raise ServiceError("Dashboard review asset is unavailable")
        return path

    def dashboard_ui_revision(self, session_id: str) -> int:
        session = self.dashboard_status(session_id)
        row = self.db.one(
            "SELECT MAX(id) AS id FROM events WHERE workflow_id = ? "
            "AND created_at >= ? AND event_type IN ("
            "'proposal_created','proposal_approved','run_started','run_analyzed',"
            "'run_failed','run_stopped','run_cancelled_by_shutdown','decision_recorded','autonomy_pause','autonomy_stop',"
            "'autonomy_emergency_stop','autonomy_halted','autonomy_lease_expired',"
            "'autonomy_scope_completed','autonomy_resumed','measurement_mode_resumed',"
            "'measurement_paused_for_instrument_error','instrument_error_paused',"
            "'workflow_stopped','workflow_quarantined',"
            "'full_shutdown_requested','hardware_lock_recovered')",
            (session["workflow_id"], session["started_at"]),
        )
        return int(row["id"] or 0) if row is not None else 0

    def dashboard_ui_events_after(
        self,
        session_id: str,
        after_id: int,
        event_types: Collection[str] | None = None,
    ) -> dict[str, Any]:
        session = self.dashboard_status(session_id)
        rows = self.db.all(
            "SELECT id, event_type FROM events WHERE workflow_id = ? "
            "AND created_at >= ? AND id > ? AND event_type IN ("
            "'proposal_created','proposal_approved','run_started','run_analyzed',"
            "'run_failed','run_stopped','run_cancelled_by_shutdown','decision_recorded','autonomy_pause','autonomy_stop',"
            "'autonomy_emergency_stop','autonomy_halted','autonomy_lease_expired',"
            "'autonomy_scope_completed','autonomy_resumed','measurement_mode_resumed',"
            "'measurement_paused_for_instrument_error','instrument_error_paused',"
            "'workflow_stopped','workflow_quarantined',"
            "'full_shutdown_requested','hardware_lock_recovered') "
            "ORDER BY id LIMIT 250",
            (session["workflow_id"], session["started_at"], int(after_id)),
        )
        selected = set(event_types) if event_types is not None else None
        return {
            "cursor": max(
                (int(row["id"]) for row in rows), default=int(after_id)
            ),
            "events": [
                {"id": int(row["id"]), "event_type": row["event_type"]}
                for row in rows
                if selected is None or row["event_type"] in selected
            ],
        }

    def approval_csrf_token(self, proposal_id: str) -> str:
        proposal = self.db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if proposal is None:
            raise ServiceError(f"Unknown proposal: {proposal_id}")
        message = "\0".join(
            (
                proposal["id"],
                proposal["workflow_id"],
                proposal["created_at"],
                proposal["expires_at"],
            )
        ).encode("utf-8")
        return hmac.new(self._approval_secret, message, hashlib.sha256).hexdigest()

    def approve_from_browser(
        self,
        proposal_id: str,
        confirmation: str,
        csrf_token: str,
        remote_host: str,
        *,
        actor: str | None = None,
        approval_method: str = "local_browser",
    ) -> dict[str, Any]:
        expected_confirmation = f"APPROVE {proposal_id}"
        if not hmac.compare_digest(confirmation, expected_confirmation):
            raise ServiceError("The confirmation text did not match exactly.")
        expected_token = self.approval_csrf_token(proposal_id)
        if not hmac.compare_digest(csrf_token, expected_token):
            raise ServiceError("The browser approval token was invalid.")
        resolved_actor = actor or (
            f"{getpass.getuser()} via local browser ({remote_host})"
        )
        return self._approve_pending_proposal(
            proposal_id,
            resolved_actor,
            approval_method,
        )

    def proposal_review(self, proposal_id: str) -> dict[str, Any]:
        """Return prior run evidence that explains the proposed next action."""
        proposal = self.db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if proposal is None:
            raise ServiceError(f"Unknown proposal: {proposal_id}")
        payload = json_loads(proposal["payload_json"], {})
        run_id = payload.get("run_id") or payload.get("review_run_id")
        if not run_id and proposal["kind"] == "run":
            run_id = self._latest_review_run_id(
                proposal["workflow_id"], str(payload.get("node_id", ""))
            )
        if not run_id:
            run_id = self._latest_workflow_decision_run_id(
                proposal["workflow_id"]
            )
        if not isinstance(run_id, str) or not run_id:
            return {
                "available": False,
                "run": None,
                "analysis": None,
                "decision": None,
                "plots": [],
            }
        return self._run_review(str(run_id), proposal["workflow_id"])

    def autonomy_review(self, lease_id: str) -> dict[str, Any]:
        """Return every run/result/decision accumulated under one authorization."""
        lease = self._autonomy_row(lease_id)
        event_revision = self.autonomy_ui_revision(lease_id)
        runs = self.db.all(
            "SELECT id FROM runs WHERE autonomy_lease_id = ? "
            "ORDER BY rowid",
            (lease_id,),
        )
        if not runs:
            return {
                "available": False,
                "run": None,
                "analysis": None,
                "decision": None,
                "plots": [],
                "history": [],
                "duration_prediction": None,
                "event_revision": event_revision,
            }
        history: list[dict[str, Any]] = []
        all_plots: list[str] = []
        for row in runs:
            review = self._run_review(str(row["id"]), lease["workflow_id"])
            run_info = review.get("run") or {}
            parameters = run_info.get("parameters", {})
            review["duration_prediction"] = self.run_telemetry(
                str(run_info.get("node_id")),
                list(parameters.get("qubits", [])),
                limit=100,
            )["prediction"]
            item_plots = list(review.get("plots", []))
            review["asset_indices"] = list(
                range(len(all_plots), len(all_plots) + len(item_plots))
            )
            all_plots.extend(item_plots)
            history.append(review)
        latest = dict(history[-1])
        latest["history"] = history
        latest["plots"] = all_plots
        latest["event_revision"] = event_revision
        return latest

    def autonomy_review_asset(self, lease_id: str, index: int) -> Path:
        review = self.autonomy_review(lease_id)
        plots = review["plots"]
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(plots)
        ):
            raise ServiceError("Unknown autonomy review asset")
        path = Path(plots[index]).resolve()
        if not self._is_allowed_review_asset(path) or not path.is_file():
            raise ServiceError("Autonomy review asset is unavailable")
        return path

    def autonomy_ui_revision(self, lease_id: str) -> int:
        """Return an event cursor captured before rendering an autonomy page."""
        lease = self._autonomy_row(lease_id)
        event_types = tuple(self._autonomy_ui_event_types())
        placeholders = ", ".join("?" for _ in event_types)
        row = self.db.one(
            f"SELECT MAX(id) AS id FROM events WHERE workflow_id = ? "
            f"AND event_type IN ({placeholders})",
            (lease["workflow_id"], *event_types),
        )
        return int(row["id"] or 0) if row is not None else 0

    def autonomy_ui_events_after(
        self, lease_id: str, after_id: int
    ) -> dict[str, Any]:
        """Return result/control events relevant to one autonomy lease."""
        lease = self._autonomy_row(lease_id)
        event_types = tuple(self._autonomy_ui_event_types())
        placeholders = ", ".join("?" for _ in event_types)
        rows = self.db.all(
            f"SELECT id, event_type, payload_json FROM events "
            f"WHERE workflow_id = ? AND id > ? "
            f"AND event_type IN ({placeholders}) ORDER BY id LIMIT 250",
            (lease["workflow_id"], int(after_id), *event_types),
        )
        cursor = max((int(row["id"]) for row in rows), default=int(after_id))
        relevant: list[dict[str, Any]] = []
        for row in rows:
            payload = json_loads(row.get("payload_json"), {})
            if payload.get("lease_id") == lease_id:
                relevant.append(
                    {"id": int(row["id"]), "event_type": row["event_type"]}
                )
                continue
            run_id = payload.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                continue
            run = self.db.one(
                "SELECT autonomy_lease_id FROM runs WHERE id = ?", (run_id,)
            )
            if run is not None and run.get("autonomy_lease_id") == lease_id:
                relevant.append(
                    {"id": int(row["id"]), "event_type": row["event_type"]}
                )
        return {"cursor": cursor, "events": relevant}

    @staticmethod
    def _autonomy_ui_event_types() -> frozenset[str]:
        return frozenset(
            {
                "run_analyzed",
                "run_failed",
                "run_stopped",
                "run_cancelled_by_shutdown",
                "run_force_stopped",
                "decision_recorded",
                "autonomy_pause",
                "autonomy_stop",
                "autonomy_emergency_stop",
                "autonomy_halted",
                "autonomy_lease_expired",
                "autonomy_scope_completed",
                "autonomy_resumed",
                "autonomy_targets_scientifically_unmeasurable",
            }
        )

    #: Nodes whose playbook recovery for a noisy-but-interior trace is the
    #: configured averaging ladder rather than a new window or power.
    SNR_LADDER_NODES = ("03a", "04", "05")

    def next_action(self, workflow_id: str) -> dict[str, Any]:
        """Say deterministically what the workflow needs next, and why.

        This is an aggregator, not a second opinion: every field comes from the
        same server state the guards already enforce -- current node, per-target
        resolution, attempt quota, the shared-first-batch rule, the registered
        deterministic setup tools, and the configured averaging ladder. When no
        registered rule covers the situation it says so and cites the playbook
        section to read, rather than inventing a parameter.
        """

        workflow = self._workflow(workflow_id)
        node_id = str(workflow["current_node"])
        sequence = list(self.settings.workflow_sequence)
        active = self._active_targets_for_node(workflow, node_id)
        resolved = self._node_target_resolution(workflow_id, node_id) & active
        incomplete = self._autonomy_incomplete_targets_for_node(workflow_id, node_id)
        unresolved = active - resolved - incomplete
        index = sequence.index(node_id) if node_id in sequence else -1
        next_node = sequence[index + 1] if 0 <= index < len(sequence) - 1 else None
        latest = self.db.one(
            "SELECT r.*, d.id AS decision_id FROM runs r "
            "LEFT JOIN decisions d ON d.run_id = r.id "
            "WHERE r.workflow_id = ? AND r.node_id = ? ORDER BY r.rowid DESC LIMIT 1",
            (workflow_id, node_id),
        )
        result: dict[str, Any] = {
            "workflow_id": workflow_id,
            "workflow_status": workflow["status"],
            "current_node": node_id,
            "next_node_in_sequence": next_node,
            "targets": {
                "active": sorted(active),
                "resolved": sorted(resolved),
                "incomplete": sorted(incomplete),
                "unresolved": sorted(unresolved),
            },
            "latest_run": (
                {
                    "run_id": latest["id"],
                    "status": latest["status"],
                    "analysis_status": latest.get("analysis_status"),
                    "parameters": json_loads(latest.get("parameters_json"), {}),
                    "has_decision": latest.get("decision_id") is not None,
                    "evidence": evidence_digest(
                        json_loads(latest.get("analysis_json"), None)
                    ),
                }
                if latest is not None
                else None
            ),
            "rules": [],
            "required_setup": [],
            "notes": [],
        }
        result["attempts"] = self._attempts_for_node(workflow_id, node_id, active)

        if workflow["status"] != "active":
            result["action"] = "blocked"
            result["reason"] = (
                f"The workflow is {workflow['status']}; resume it before scheduling."
            )
            return result
        if latest is not None and latest["status"] in {
            "starting",
            "running",
            "stopping",
        }:
            result["action"] = "wait"
            result["reason"] = (
                f"Run {latest['id']} is {latest['status']}. Wait for a terminal "
                "status before analyzing."
            )
            return result
        if (
            latest is not None
            and latest["status"] == "completed"
            and latest.get("analysis_status")
            not in {"pass", "needs_review", "failed"}
        ):
            # `analysis_status` defaults to 'not_started', so test the three
            # analyzed values rather than NULL.
            result["action"] = "analyze"
            result["reason"] = f"Run {latest['id']} completed and is not analyzed yet."
            return result
        if latest is not None and not latest.get("decision_id"):
            result["action"] = "record_decision"
            result["reason"] = (
                f"Run {latest['id']} has no recorded decision. A node cannot be "
                "left without one; see 'Record `advance`, not `manual_review`, "
                "on the run that finishes a node'."
            )
            result["rules"].append(
                "PLAYBOOK: Record `advance`, not `manual_review`, on the run "
                "that finishes a node"
            )
            return result

        single_pass_done = self._node_is_single_pass(node_id) and not (
            active - self._single_pass_measured_targets(workflow, node_id)
        ) and self._node_has_completed_run(workflow_id, node_id)
        if single_pass_done:
            repeat_targets = self._active_reset_repeat_targets(workflow)
            if repeat_targets and self._is_active_reset_repeat(
                workflow, node_id, {"reset_type_thermal_or_active": "active"}
            ):
                batch = sorted(repeat_targets)
                result["action"] = "run"
                result["run"] = {
                    "node_id": node_id,
                    "qubits": batch,
                    "parameters": {
                        "qubits": batch,
                        "reset_type_thermal_or_active": "active",
                    },
                }
                result["reason"] = (
                    f"{len(batch)} target(s) cleared the active-reset trigger "
                    "fidelity on the thermal run, so repeat 07b once with "
                    "active reset. That repeat is their accepted result and "
                    "qualifies them to use active reset downstream."
                )
                result["rules"].append(
                    "PLAYBOOK: 07d and 07b run once on defaults"
                )
                return result
            result["action"] = "advance"
            result["reason"] = (
                f"{node_id} runs once on the node defaults and then advances. "
                "Its run is complete and analyzed, so record `advance` and move "
                "to the next node; do not retry or adjust parameters."
            )
            result["rules"].append(
                "PLAYBOOK: 07d and 07b run once on defaults"
            )
            result["run"] = None
            return result

        result["required_setup"] = self._required_setup_for_node(
            node_id, sorted(active), workflow_id
        )
        if result["required_setup"]:
            result["action"] = "setup"
            result["reason"] = (
                "A registered deterministic setup must be applied before this "
                "node can run."
            )
            return result

        if not unresolved:
            result["action"] = "advance"
            result["reason"] = (
                f"Every active {node_id} target is resolved or incomplete at its "
                "scientific boundary."
                if incomplete
                else f"Every active {node_id} target is resolved."
            )
            result["run"] = None
            return result

        cap = self._node_multiplex_cap(node_id)
        first_batch = node_id in SHARED_FIRST_BATCH_NODES and not (
            self._node_has_completed_run(workflow_id, node_id)
        )
        if first_batch and not (cap is not None and len(active) > cap):
            result["action"] = "run"
            result["run"] = {
                "node_id": node_id,
                "qubits": sorted(active),
                "parameters": {"qubits": sorted(active)},
            }
            result["reason"] = (
                f"The first {node_id} run must multiplex every active target "
                "with one shared parameter set; node defaults apply to the rest."
            )
            result["rules"].append("PLAYBOOK: Multiplex workflows")
            return result

        batch = sorted(unresolved)
        capped = cap is not None and len(batch) > cap
        if capped:
            batch = batch[:cap]
        result["action"] = "run"
        result["run"] = {
            "node_id": node_id,
            "qubits": batch,
            "parameters": {"qubits": batch},
        }
        result["reason"] = (
            f"{node_id} multiplexes at most {cap} targets per run; take the "
            f"next group of {len(batch)} and repeat the node for the rest."
            if capped
            else f"Retry the unresolved {node_id} targets together; omit the "
            "resolved ones."
        )
        result["rules"].append("PLAYBOOK: Multiplex workflows")
        ladder = self._ladder_recommendation(latest, node_id, unresolved)
        if ladder is not None:
            result["run"]["parameters"] = ladder["parameters"]
            result["run"]["qubits"] = ladder["parameters"]["qubits"]
            result["reason"] = ladder["reason"]
            result["rules"].append(ladder["rule"])
        else:
            result["notes"].append(
                "No registered deterministic parameter change covers this "
                f"situation. Read the '{node_id}' section of the playbook and "
                f"`rules/experiences/{node_id}.md` before choosing parameters."
            )
        return result

    def _attempts_for_node(
        self, workflow_id: str, node_id: str, targets: Collection[str]
    ) -> dict[str, Any]:
        """Per-target attempts used and left under the current lease."""

        lease = self.db.one(
            "SELECT * FROM autonomy_leases WHERE workflow_id = ? "
            "AND status IN ('active', 'paused') ORDER BY created_at DESC LIMIT 1",
            (workflow_id,),
        )
        if lease is None:
            return {"lease_id": None, "used": {}, "remaining": {}, "maximum": None}
        maximum = int(lease["max_attempts_per_node_qubit"])
        used = {str(name): 0 for name in targets}
        for run in self.db.all(
            "SELECT parameters_json FROM runs WHERE autonomy_lease_id = ? "
            "AND node_id = ?",
            (lease["id"], node_id),
        ):
            prior = set(json_loads(run.get("parameters_json"), {}).get("qubits", []))
            for name in used:
                if name in prior:
                    used[name] += 1
        return {
            "lease_id": lease["id"],
            "maximum": maximum,
            "used": used,
            "remaining": {
                name: max(0, maximum - count) for name, count in used.items()
            },
        }

    def _required_setup_for_node(
        self, node_id: str, targets: list[str], workflow_id: str
    ) -> list[dict[str, str]]:
        """The deterministic setup tools the playbook requires before a node."""

        required: list[dict[str, str]] = []
        try:
            state = load_state(self.settings.active_state)
        except Exception:
            return required
        qubits = state.get("qubits", {}) if isinstance(state, dict) else {}
        if node_id in {"03a", "04", "05"}:
            missing_x180 = [
                name
                for name in targets
                if isinstance(qubits.get(name), dict)
                and operation_amplitude(qubits[name], "x180") == 0
            ]
            if missing_x180:
                required.append(
                    {
                        "tool": "jy_request_bootstrap",
                        "targets": ", ".join(missing_x180),
                        "why": (
                            "x180 amplitude is zero; 03a/04/05 cannot drive the "
                            "qubit until bootstrap is applied."
                        ),
                    }
                )
        if node_id == "03a" and not self._node_has_completed_run(workflow_id, "03a"):
            required.append(
                {
                    "tool": "jy_request_initial_03a_zero_if",
                    "targets": ", ".join(targets),
                    "why": (
                        "The first 03a coarse search requires every target's "
                        "private LO on its RF with XY IF at zero."
                    ),
                }
            )
        if node_id == "07b":
            missing_fidelity = [
                name
                for name in targets
                if "readout_fidelity"
                not in (qubits.get(name, {}).get("extras", {}) or {})
            ]
            if missing_fidelity:
                required.append(
                    {
                        "tool": "jy_request_07b_prerequisites",
                        "targets": ", ".join(missing_fidelity),
                        "why": (
                            "extras/readout_fidelity is missing, so Qualibrate's "
                            "state recorder cannot observe an old value."
                        ),
                    }
                )
        return required

    def _ladder_recommendation(
        self, latest: dict[str, Any] | None, node_id: str, unresolved: set[str]
    ) -> dict[str, Any] | None:
        """The configured averaging ladder, when it applies to this retry."""

        if latest is None or node_id not in self.SNR_LADDER_NODES:
            return None
        if latest["status"] != "completed" or not latest.get("analysis_json"):
            return None
        try:
            guidance = self.snr_retry_recommendation(str(latest["id"]))
        except ServiceError:
            return None
        if not guidance.get("retry_recommended"):
            return None
        low_snr = set(guidance.get("low_snr_targets", []))
        if not low_snr or not low_snr <= unresolved:
            return None
        parameters = dict(guidance["retry_parameters"])
        parameters["qubits"] = sorted(low_snr)
        return {
            "parameters": parameters,
            "reason": guidance["reason"],
            "rule": (
                f"policies.yaml analysis.{node_id}.noise_confirmation_num_averages"
            ),
        }

    def snr_retry_recommendation(self, run_id: str) -> dict[str, Any]:
        """Recommend the next safe averaging step for a noisy 03a/04/05 result."""
        run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if run is None:
            raise ServiceError(f"Unknown run: {run_id}")
        node_id = str(run["node_id"])
        if node_id not in self.SNR_LADDER_NODES:
            raise ServiceError(
                "SNR retry guidance is available only for nodes "
                f"{', '.join(self.SNR_LADDER_NODES)}"
            )
        analysis = json_loads(run.get("analysis_json"), {})
        if run["status"] != "completed" or not analysis:
            raise ServiceError(
                f"Analyze a completed {node_id} run before requesting guidance"
            )
        parameters = json_loads(run.get("parameters_json"), {})
        rules = self.policy.raw["analysis"][node_id]
        minimum = float(rules["min_robust_snr"])
        metrics = analysis.get("dataset_metrics", {}).get("qubits", {})
        requested = [str(name) for name in parameters.get("qubits", [])]
        low_snr: list[str] = []
        observed: dict[str, float | None] = {}
        for name in requested:
            item = metrics.get(name, {}) if isinstance(metrics, dict) else {}
            value = item.get("robust_snr") if isinstance(item, dict) else None
            observed[name] = float(value) if _finite_number(value) else None
            if not _finite_number(value) or float(value) < minimum:
                low_snr.append(name)
        current = parameters.get("num_averages")
        current_averages = (
            int(current)
            if isinstance(current, int) and not isinstance(current, bool)
            else 0
        )
        ladder = sorted(
            {
                int(value)
                for value in rules["noise_confirmation_num_averages"]
                if int(value) <= int(rules["max_noise_confirmation_num_averages"])
            }
        )
        next_averages = next(
            (value for value in ladder if value > current_averages), None
        )
        retry_parameters = dict(parameters)
        retry_parameters["qubits"] = low_snr
        if next_averages is not None:
            retry_parameters["num_averages"] = next_averages
            retry_parameters["load_data_id"] = None
            retry_parameters["simulate"] = False
        retry_recommended = bool(low_snr and next_averages is not None)
        return {
            "run_id": run_id,
            "node_id": node_id,
            "minimum_robust_snr": minimum,
            "observed_robust_snr": observed,
            "low_snr_targets": low_snr,
            "current_num_averages": current_averages,
            "next_num_averages": next_averages,
            "retry_recommended": retry_recommended,
            "retry_parameters": retry_parameters if retry_recommended else {},
            "reason": (
                f"Repeat only {low_snr} with num_averages={next_averages}; "
                f"the {node_id} completion floor is robust_snr >= {minimum:g}."
                if retry_recommended
                else "All requested targets meet the SNR floor."
                if not low_snr
                else "The configured averaging confirmation ceiling is reached; "
                "do not mark these targets complete from SNR evidence."
            ),
        }

    def _run_review(self, run_id: str, workflow_id: str) -> dict[str, Any]:
        run = self.db.one(
            "SELECT * FROM runs WHERE id = ? AND workflow_id = ?",
            (run_id, workflow_id),
        )
        if run is None:
            return {
                "available": False,
                "run": None,
                "analysis": None,
                "decision": None,
                "plots": [],
            }
        analysis = json_loads(run.get("analysis_json"), None)
        decision_row = self.db.one(
            "SELECT * FROM decisions WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        )
        decision = None
        if decision_row is not None:
            decision = dict(decision_row)
            decision["next_parameters"] = json_loads(
                decision.pop("next_parameters_json"), {}
            )
            decision["state_patch"] = json_loads(
                decision.pop("state_patch_json"), []
            )
        plots: list[str] = []
        if isinstance(analysis, dict):
            for value in analysis.get("plots", []):
                if isinstance(value, str) and self._is_allowed_review_asset(
                    Path(value)
                ):
                    plots.append(str(Path(value).resolve()))
        return {
            "available": bool(analysis or decision or plots),
            "run": {
                "id": run["id"],
                "node_id": run["node_id"],
                "status": run["status"],
                "parameters": json_loads(run.get("parameters_json"), {}),
                "started_at": run.get("started_at"),
                "finished_at": run.get("finished_at"),
                "elapsed_seconds": _elapsed_seconds(run),
                "snapshot_id": run.get("snapshot_id"),
                "snapshot_path": run.get("snapshot_path"),
                "analysis_status": run.get("analysis_status"),
                # The Dashboard has no policy access, so tell it whether this
                # node is one that runs once on defaults; its result summary
                # must not report such a run as unsuccessful.
                "single_pass": self._node_is_single_pass(str(run["node_id"])),
            },
            "analysis": analysis,
            "decision": decision,
            "plots": plots,
        }

    def proposal_review_asset(self, proposal_id: str, index: int) -> Path:
        review = self.proposal_review(proposal_id)
        plots = review["plots"]
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(plots)
        ):
            raise ServiceError("Unknown proposal review asset")
        path = Path(plots[index]).resolve()
        if not self._is_allowed_review_asset(path) or not path.is_file():
            raise ServiceError("Proposal review asset is unavailable")
        return path

    def _is_allowed_review_asset(self, path: Path) -> bool:
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            return False
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return False
        allowed_roots = (
            self.settings.data_root.resolve(),
            (self.settings.runtime / "report_assets").resolve(),
        )
        return any(
            resolved == root or root in resolved.parents for root in allowed_roots
        )

    def _approve_pending_proposal(
        self,
        proposal_id: str,
        actor: str,
        approval_method: str,
    ) -> dict[str, Any]:
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ServiceError(f"Unknown proposal: {proposal_id}")
            proposal = dict(row)
            if proposal["status"] != "pending":
                raise ServiceError(
                    f"Proposal status is {proposal['status']}, not pending"
                )
            if datetime.fromisoformat(proposal["expires_at"]) <= datetime.now(
                timezone.utc
            ):
                connection.execute(
                    "UPDATE proposals SET status = 'expired' "
                    "WHERE id = ? AND status = 'pending'",
                    (proposal_id,),
                )
                raise ServiceError("Proposal has expired")
            changed = connection.execute(
                "UPDATE proposals SET status = 'approved', approved_at = ?, "
                "approved_by = ? WHERE id = ? AND status = 'pending'",
                (utc_now(), actor, proposal_id),
            ).rowcount
            if changed != 1:
                raise ServiceError("Proposal approval was resolved concurrently")
            if proposal["kind"] == "autonomy_lease":
                self._activate_autonomy_lease(
                    proposal, actor, connection=connection
                )
            self.db.event(
                "proposal_approved",
                actor,
                {
                    "proposal_id": proposal_id,
                    "kind": proposal["kind"],
                    "approval_method": approval_method,
                },
                proposal["workflow_id"],
                connection=connection,
            )
        return self.proposal(proposal_id)

    def _load_or_create_approval_secret(self) -> bytes:
        secret_path = self.settings.runtime / "browser_approval.secret"
        try:
            with secret_path.open("xb") as handle:
                handle.write(os.urandom(32))
            try:
                os.chmod(secret_path, 0o600)
            except OSError:
                pass
        except FileExistsError:
            pass
        secret = secret_path.read_bytes()
        if len(secret) < 32:
            raise ServiceError("Browser approval secret is invalid")
        return secret

    def list_historical_runs(
        self, node_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if node_id is not None:
            definition = self.policy.node_definition(node_id)
            needle = definition["name"]
        else:
            needle = ""
        candidates: list[dict[str, Any]] = []
        for node_file in self.settings.data_root.rglob("node.json"):
            if needle and needle.lower() not in node_file.parent.name.lower():
                continue
            try:
                raw = json.loads(node_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            candidates.append(
                {
                    "snapshot_id": raw.get("id"),
                    "name": raw.get("metadata", {}).get("name"),
                    "created_at": raw.get("created_at"),
                    "path": str(node_file.parent),
                    "outcomes": raw.get("data", {}).get("outcomes", {}),
                }
            )
        candidates.sort(key=lambda item: item.get("snapshot_id") or -1, reverse=True)
        return candidates[: max(1, min(int(limit), 100))]

    def _assert_state_commit_idle(self) -> None:
        active = self.db.one(
            "SELECT id FROM runs WHERE status IN ('starting', 'running', 'stopping') "
            "ORDER BY started_at DESC LIMIT 1"
        )
        if active is not None:
            raise ServiceError(
                f"State commits are blocked while run {active['id']} is active"
            )
        if self.settings.lock_path.exists():
            raise ServiceError(
                "State commits are blocked while the JY hardware lock exists"
            )

    def _create_proposal(
        self,
        workflow_id: str,
        kind: str,
        payload: dict[str, Any],
        source_client: str,
        ttl_minutes: int,
        max_uses: int,
        autonomy_lease_id: str | None = None,
    ) -> dict[str, Any]:
        if kind == "state_commit":
            self._assert_state_commit_idle()
        proposal_id = uuid.uuid4().hex
        created = datetime.now(timezone.utc)
        expires = created + timedelta(minutes=ttl_minutes)
        self.db.execute(
            """
            INSERT INTO proposals(
                id, workflow_id, kind, payload_json, status, created_at,
                expires_at, max_uses, source_client, autonomy_lease_id
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                workflow_id,
                kind,
                json_dumps(payload),
                created.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
                max_uses,
                source_client,
                autonomy_lease_id,
            ),
        )
        self.db.event(
            "proposal_created",
            source_client,
            {
                "proposal_id": proposal_id,
                "kind": kind,
                "max_uses": max_uses,
                "autonomy_lease_id": autonomy_lease_id,
            },
            workflow_id,
        )
        return self.proposal(proposal_id)

    @property
    def autonomy_mode_entry_phrase(self) -> str:
        return self.settings.autonomy_mode_entry_phrase

    def autonomy_control_url(self, lease_id: str) -> str:
        session = self.db.one(
            "SELECT id FROM measurement_sessions WHERE autonomy_lease_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (lease_id,),
        )
        if session is not None:
            return self.session_dashboard_url(str(session["id"]))
        # Compatibility fallback for databases created before every lease was
        # guaranteed to have a Dashboard session.  The legacy route redirects
        # to the canonical session whenever a binding exists.
        return self._external_browser_url(f"/autonomy/{lease_id}")

    def autonomy_csrf_token(self, lease_id: str) -> str:
        lease = self._autonomy_row(lease_id)
        message = "\0".join(
            (
                lease["id"],
                lease["workflow_id"],
                lease["proposal_id"],
                lease["created_at"],
            )
        ).encode("utf-8")
        return hmac.new(self._approval_secret, message, hashlib.sha256).hexdigest()

    def dashboard_shutdown_csrf_token(self, session_id: str) -> str:
        session = self.db.one(
            "SELECT * FROM measurement_sessions WHERE id = ?", (session_id,)
        )
        if session is None:
            raise ServiceError("Unknown measurement dashboard session")
        message = "\0".join(
            (
                "full-shutdown",
                session["id"],
                session["workflow_id"],
                session["started_at"],
            )
        ).encode("utf-8")
        return hmac.new(self._approval_secret, message, hashlib.sha256).hexdigest()

    def dashboard_shutdown_confirmation(self, session_id: str) -> str:
        self.dashboard_status(session_id)
        return f"SHUTDOWN {session_id}"

    def operator_csrf_token(self) -> str:
        nonce = os.getenv("JY_SERVICE_INSTANCE_NONCE", "local-development")
        return hmac.new(
            self._approval_secret,
            f"operator-console\0{nonce}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def request_full_shutdown_from_browser(
        self,
        session_id: str,
        confirmation: str,
        csrf_token: str,
        actor: str,
    ) -> dict[str, Any]:
        if not hmac.compare_digest(
            confirmation, self.dashboard_shutdown_confirmation(session_id)
        ):
            raise ServiceError("The full-shutdown confirmation did not match exactly.")
        if not hmac.compare_digest(
            csrf_token, self.dashboard_shutdown_csrf_token(session_id)
        ):
            raise ServiceError("The full-shutdown control token was invalid.")
        return self.request_full_shutdown(session_id, actor)

    def request_full_shutdown(
        self,
        session_id: str,
        actor: str,
    ) -> dict[str, Any]:
        if not actor.strip():
            raise ServiceError("Shutdown actor is required")
        session = self.db.one(
            "SELECT * FROM measurement_sessions WHERE id = ?", (session_id,)
        )
        if session is None:
            raise ServiceError("Unknown measurement dashboard session")
        workflow = self._workflow(str(session["workflow_id"]))
        other_open = self.db.one(
            "SELECT id FROM workflows WHERE id != ? AND status IN ('active','paused') "
            "LIMIT 1",
            (workflow["id"],),
        )
        if other_open is not None:
            raise ServiceError(
                "This is an older Dashboard session; refuse to shut down services "
                "while another workflow is open."
            )
        active_run = self.db.one(
            "SELECT * FROM runs WHERE workflow_id = ? "
            "AND status IN ('starting','running','stopping') "
            "ORDER BY started_at DESC LIMIT 1",
            (workflow["id"],),
        )
        status = "waiting_for_run" if active_run is not None else "requested"
        now = datetime.now(timezone.utc)
        grace = int(
            self.policy.raw.get("autonomy", {}).get(
                "emergency_stop_grace_seconds", 30
            )
        )
        request = {
            "session_id": session_id,
            "workflow_id": workflow["id"],
            "requested_by": actor.strip(),
            "requested_at": now.isoformat(timespec="seconds"),
            "dispatch_after": (now + timedelta(seconds=3)).isoformat(
                timespec="seconds"
            ),
            "force_after": (now + timedelta(seconds=max(0, grace))).isoformat(
                timespec="seconds"
            ),
            "instance_nonce": os.getenv(
                "JY_SERVICE_INSTANCE_NONCE", "local-development"
            ),
            "status": status,
            "active_run_id": active_run.get("id") if active_run else None,
            "operator_console_url": self.operator_console_url,
        }
        self.db.execute(
            "UPDATE measurement_sessions SET status = 'stopping', updated_at = ? "
            "WHERE id = ?",
            (utc_now(), session_id),
        )
        self.db.event(
            "full_shutdown_requested",
            actor.strip(),
            {
                "session_id": session_id,
                "active_run_id": request["active_run_id"],
                "status": status,
            },
            workflow["id"],
        )
        # Persist the operator's intent before touching the worker.  A delivery
        # failure must never erase a valid request to close the workflow/services.
        self._save_shutdown_request(request)
        if active_run is not None:
            try:
                request["stop_result"] = self.runner.stop(
                    str(active_run["id"]),
                    actor.strip(),
                    intent="full_shutdown",
                    reason="Full measurement shutdown requested from Dashboard.",
                )
            except Exception as exc:
                request["stop_error"] = f"{type(exc).__name__}: {exc}"
            self._save_shutdown_request(request)
        return self._reconcile_full_shutdown_request(session_id) or request

    @property
    def _full_shutdown_request_path(self) -> Path:
        """Legacy latest-request mirror retained for diagnostics and upgrades."""
        return self.settings.runtime / "full-shutdown-request.json"

    def _shutdown_request_path(self, session_id: str) -> Path:
        return self.settings.runtime / "shutdown_requests" / f"{session_id}.json"

    def _save_shutdown_request(self, request: dict[str, Any]) -> None:
        session_id = str(request.get("session_id") or "")
        workflow_id = str(request.get("workflow_id") or "")
        if not session_id or not workflow_id:
            raise ServiceError("Shutdown request is missing its session/workflow id")
        now = utc_now()
        payload = dict(request)
        atomic_write_json(self._shutdown_request_path(session_id), payload)
        # Keep one human-readable latest mirror for older diagnostics.  Reconcile
        # never relies on it, so one session cannot consume another's request.
        atomic_write_json(self._full_shutdown_request_path, payload)
        self.db.execute(
            """
            INSERT INTO shutdown_requests(
                session_id, workflow_id, status, requested_by, requested_at,
                updated_at, active_run_id, stop_error, quarantine, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at,
                active_run_id = excluded.active_run_id,
                stop_error = excluded.stop_error,
                quarantine = excluded.quarantine,
                payload_json = excluded.payload_json
            """,
            (
                session_id,
                workflow_id,
                str(payload.get("status") or "requested"),
                str(payload.get("requested_by") or "unknown"),
                str(payload.get("requested_at") or now),
                now,
                payload.get("active_run_id"),
                payload.get("stop_error"),
                int(bool(payload.get("quarantine"))),
                json_dumps(payload),
            ),
        )

    def _load_shutdown_request(
        self, session_id: str | None = None
    ) -> dict[str, Any] | None:
        if session_id:
            row = self.db.one(
                "SELECT payload_json FROM shutdown_requests WHERE session_id = ?",
                (session_id,),
            )
        else:
            row = self.db.one(
                "SELECT payload_json FROM shutdown_requests "
                "ORDER BY updated_at DESC LIMIT 1"
            )
        if row is None:
            return None
        request = json_loads(row.get("payload_json"), None)
        return request if isinstance(request, dict) else None

    def _shutdown_request_for_session(self, session_id: str) -> dict[str, Any] | None:
        request = self._load_shutdown_request(session_id)
        if request is None:
            return None
        request = dict(request)
        request.pop("instance_nonce", None)
        return request

    def _reconcile_full_shutdown_request(
        self, session_id: str | None = None
    ) -> dict[str, Any] | None:
        request = self._load_shutdown_request(session_id)
        if request is None:
            return None
        expected_nonce = os.getenv("JY_SERVICE_INSTANCE_NONCE", "local-development")
        if request.get("instance_nonce") != expected_nonce:
            if (
                bool(request.get("quarantine"))
                and request.get("status") == "dispatching"
                and not self.settings.lock_path.exists()
            ):
                # A prior service instance closed successfully with a retained
                # lock.  A later recovery-only instance may finish that exact
                # session after formal operator recovery.
                workflow_id = str(request.get("workflow_id") or "")
                recovered_session_id = str(request.get("session_id") or "")
                self._finalize_workflow_shutdown(
                    workflow_id,
                    recovered_session_id,
                    "hardware-lock-recovery",
                    recovery_required=False,
                )
                request["instance_nonce"] = expected_nonce
                request["status"] = "ready"
                request["recovered_at"] = utc_now()
                request["dispatch_after"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=5)
                ).isoformat(timespec="seconds")
                self._save_shutdown_request(request)
                return request
            return None
        if request.get("status") == "dispatching":
            failure_path = (
                self.settings.runtime
                / "shutdown-stop-failures"
                / f"{request.get('session_id')}.json"
            )
            try:
                failure = json.loads(failure_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, TypeError):
                return request
            request["status"] = "failed"
            request["error"] = str(
                failure.get("error") or "The stop helper failed without details."
            )
            request["stop_helper_failure"] = failure
            self._save_shutdown_request(request)
            return request
        if request.get("status") == "failed":
            return request

        workflow_id = str(request.get("workflow_id") or "")
        session_id = str(request.get("session_id") or "")
        active_run = self.db.one(
            "SELECT * FROM runs WHERE workflow_id = ? "
            "AND status IN ('starting','running','stopping') "
            "ORDER BY started_at DESC LIMIT 1",
            (workflow_id,),
        )
        if active_run is None and request.get("active_run_id"):
            prior = self.db.one(
                "SELECT * FROM runs WHERE id = ?",
                (str(request["active_run_id"]),),
            )
            prior_pid = prior.get("pid") if prior else None
            if (
                prior is not None
                and isinstance(prior_pid, int)
                and self.runner.process_is_alive(prior_pid)
            ):
                active_run = prior
        if active_run is not None:
            request["status"] = "waiting_for_run"
            request["active_run_id"] = active_run["id"]
            pid = active_run.get("pid")
            if not isinstance(pid, int):
                # A just-created run may be between its DB insert and Popen PID
                # update.  Keep the shutdown request durable and let the next
                # sweep observe the exact process identity.
                try:
                    missing_pid_deadline = parse_iso_datetime(
                        str(request.get("force_after") or "")
                    )
                    if missing_pid_deadline.tzinfo is None:
                        missing_pid_deadline = missing_pid_deadline.replace(
                            tzinfo=timezone.utc
                        )
                except (TypeError, ValueError):
                    missing_pid_deadline = datetime.now(timezone.utc) + timedelta(
                        seconds=30
                    )
                if missing_pid_deadline > datetime.now(timezone.utc):
                    self._save_shutdown_request(request)
                    return request
                self.db.execute(
                    "UPDATE runs SET status = 'failed', finished_at = ?, error = ?, "
                    "termination_cause = 'missing_process_identity_during_shutdown' "
                    "WHERE id = ?",
                    (
                        utc_now(),
                        "Shutdown grace elapsed before the worker PID was recorded.",
                        active_run["id"],
                    ),
                )
            if isinstance(pid, int) and self.runner.process_is_alive(pid):
                try:
                    force_after = parse_iso_datetime(str(request.get("force_after") or ""))
                    if force_after.tzinfo is None:
                        force_after = force_after.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    force_after = datetime.now(timezone.utc) + timedelta(seconds=30)
                if force_after <= datetime.now(timezone.utc):
                    try:
                        request["force_stop_result"] = self.runner.force_stop(
                            str(active_run["id"]), "full-shutdown-coordinator"
                        )
                        request["status"] = "waiting_for_force_exit"
                    except Exception as exc:
                        request["stop_error"] = f"{type(exc).__name__}: {exc}"
                self._save_shutdown_request(request)
                return request

            evidence = self.runner.exit_evidence(str(active_run["id"]))
            stop_intent = str(
                active_run.get("stop_intent")
                or evidence.get("stop_request", {}).get("intent")
                or ""
            )
            refreshed = self.db.one(
                "SELECT * FROM runs WHERE id = ?", (active_run["id"],)
            )
            if refreshed and refreshed["status"] in {"starting", "running", "stopping"}:
                terminal_status = (
                    "cancelled_by_shutdown"
                    if stop_intent == "full_shutdown"
                    else "stopped"
                )
                cause = str(
                    evidence.get("exit_receipt", {}).get("termination_cause")
                    or "worker_exit_during_shutdown"
                )
                message = (
                    "Worker exited while full shutdown was in progress; "
                    "cleanup is determined from the retained lock and exit receipt."
                )
                self.db.execute(
                    "UPDATE runs SET status = ?, finished_at = ?, error = ?, "
                    "termination_cause = ? WHERE id = ?",
                    (terminal_status, utc_now(), message, cause, active_run["id"]),
                )
                self.db.event(
                    "run_cancelled_by_shutdown",
                    "full-shutdown-coordinator",
                    {
                        "run_id": active_run["id"],
                        "exit_evidence": evidence,
                        "hardware_lock_retained": self.settings.lock_path.exists(),
                    },
                    workflow_id,
                )
        if request.get("active_run_id"):
            request["last_run_id"] = request["active_run_id"]
        request["active_run_id"] = None
        previous_status = str(request.get("status") or "")
        if self.settings.lock_path.exists():
            request["status"] = "quarantine_ready"
            request["quarantine"] = True
            request["operator_console_url"] = self.operator_console_url
            self._finalize_workflow_shutdown(
                workflow_id,
                session_id,
                str(request.get("requested_by") or "full-shutdown-coordinator"),
                recovery_required=True,
            )
            atomic_write_json(
                self.settings.runtime / "recovery-required.json",
                {
                    "session_id": session_id,
                    "workflow_id": workflow_id,
                    "run_id": request.get("last_run_id"),
                    "reason": "Hardware lock retained after workflow shutdown.",
                    "created_at": utc_now(),
                },
            )
        else:
            self._finalize_workflow_shutdown(
                workflow_id,
                session_id,
                str(request.get("requested_by") or "full-shutdown-coordinator"),
                recovery_required=False,
            )
            request["status"] = "ready"
            request["quarantine"] = False
        if previous_status not in {"ready", "quarantine_ready"}:
            # Give the browser/operator response time to reach the user before
            # the helper stops the very HTTP services that delivered it.  The
            # MCP watchdog will reconcile the ready request again.
            ready_at = datetime.now(timezone.utc)
            request["ready_at"] = ready_at.isoformat(timespec="seconds")
            request["dispatch_after"] = (
                ready_at + timedelta(seconds=5)
            ).isoformat(timespec="seconds")
        self._save_shutdown_request(request)
        try:
            dispatch_after = parse_iso_datetime(request.get("dispatch_after", ""))
            if dispatch_after.tzinfo is None:
                dispatch_after = dispatch_after.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            dispatch_after = datetime.now(timezone.utc)
        if dispatch_after > datetime.now(timezone.utc):
            return request

        dispatch_lock = self.settings.runtime / "full-shutdown-dispatch.lock"
        try:
            with exclusive_file_lock(dispatch_lock, "full service shutdown dispatch"):
                current = self._load_shutdown_request(session_id) or request
                if current.get("status") not in {"ready", "quarantine_ready"}:
                    return current
                dispatch_mode = str(current["status"])
                current["status"] = "dispatching"
                current["dispatch_mode"] = dispatch_mode
                current["dispatched_at"] = utc_now()
                self._save_shutdown_request(current)
                self.db.event(
                    "full_shutdown_dispatched",
                    "full-shutdown-coordinator",
                    {"session_id": session_id},
                    workflow_id,
                )
                command = [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.settings.agent_root / "stop_server.ps1"),
                    "-SessionId",
                    session_id,
                ]
                (
                    self.settings.runtime
                    / "shutdown-stop-failures"
                    / f"{session_id}.json"
                ).unlink(missing_ok=True)
                creation_flags = 0
                if os.name == "nt":
                    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                subprocess.Popen(
                    command,
                    cwd=self.settings.agent_root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
                return current
        except Exception as exc:
            request["status"] = "failed"
            request["error"] = f"{type(exc).__name__}: {exc}"
            self._save_shutdown_request(request)
            self.db.event(
                "full_shutdown_dispatch_failed",
                "full-shutdown-coordinator",
                {"session_id": session_id, "error": request["error"]},
                workflow_id,
            )
            return request

    def reconcile_full_shutdown_request(
        self, session_id: str | None = None
    ) -> dict[str, Any] | None:
        """Re-check a queued Dashboard shutdown after local operator recovery."""
        return self._reconcile_full_shutdown_request(session_id)

    def _finalize_workflow_shutdown(
        self,
        workflow_id: str,
        session_id: str,
        actor: str,
        *,
        recovery_required: bool = False,
    ) -> None:
        now = utc_now()
        final_status = "recovery_required" if recovery_required else "stopped"
        event_type = "workflow_quarantined" if recovery_required else "workflow_stopped"
        with self.db.transaction(immediate=True) as connection:
            workflow = connection.execute(
                "SELECT status FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise ServiceError("Shutdown workflow no longer exists")
            changed = connection.execute(
                "UPDATE workflows SET status = ?, updated_at = ? "
                "WHERE id = ? AND status IN ('active','paused','recovery_required')",
                (final_status, now, workflow_id),
            ).rowcount
            connection.execute(
                "UPDATE measurement_sessions SET status = ?, updated_at = ? "
                "WHERE id = ?",
                (final_status, now, session_id),
            )
            connection.execute(
                "UPDATE autonomy_leases SET status = 'revoked', stopped_reason = ?, "
                "updated_at = ? WHERE workflow_id = ? "
                "AND status IN ('pending','active','paused')",
                ("Full measurement shutdown requested from Dashboard.", now, workflow_id),
            )
            connection.execute(
                "UPDATE proposals SET status = 'cancelled' WHERE workflow_id = ? "
                "AND status = 'pending'",
                (workflow_id,),
            )
            if changed:
                self.db.event(
                    event_type,
                    actor,
                    {
                        "workflow_id": workflow_id,
                        "reason": "Full measurement shutdown requested from Dashboard.",
                        "recovery_required": recovery_required,
                    },
                    workflow_id,
                    connection=connection,
                )

    def control_autonomy_from_browser(
        self,
        lease_id: str,
        action: str,
        csrf_token: str,
        actor: str,
    ) -> dict[str, Any]:
        if not hmac.compare_digest(csrf_token, self.autonomy_csrf_token(lease_id)):
            raise ServiceError("The autonomy control token was invalid.")
        reason = f"Manual {action.replace('_', ' ')} requested from control page."
        if action == "pause":
            return self.pause_autonomy(lease_id, actor, reason)
        if action == "resume":
            return self.resume_autonomy(
                lease_id,
                actor,
                self.settings.autonomy_mode_entry_phrase,
            )
        if action == "stop":
            return self.stop_autonomy(lease_id, actor, reason)
        if action == "emergency_stop":
            return self.stop_autonomy(lease_id, actor, reason, emergency=True)
        raise ServiceError("Unknown autonomy control action")

    def start_autonomy_watchdog(self) -> None:
        """Start an idempotent daemon that enforces expiry and hard-stop rules."""
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()

        def watch() -> None:
            interval = float(
                self.policy.raw["autonomy"].get("watchdog_interval_seconds", 2)
            )
            while not self._watchdog_stop.wait(max(0.25, interval)):
                try:
                    self.autonomy_watchdog_sweep()
                except Exception as exc:
                    self.db.event(
                        "autonomy_watchdog_error",
                        "autonomy_watchdog",
                        {"error": str(exc)},
                    )

        self._watchdog_thread = threading.Thread(
            target=watch,
            name="jy-autonomy-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def stop_autonomy_watchdog(self) -> None:
        self._watchdog_stop.set()

    def autonomy_watchdog_sweep(self) -> dict[str, int]:
        expired = 0
        halted = 0
        emergency_forced = self._enforce_pending_emergency_stops()
        for lease in self.db.all(
            "SELECT * FROM autonomy_leases WHERE status IN ('active', 'paused')"
        ):
            refreshed = self._expire_autonomy_if_needed(lease)
            if refreshed["status"] == "expired":
                expired += 1
                continue
            runs = self.db.all(
                "SELECT * FROM runs WHERE autonomy_lease_id = ? "
                "ORDER BY started_at DESC LIMIT 20",
                (lease["id"],),
            )
            for run in runs:
                before = self._autonomy_row(lease["id"])["status"]
                self._enforce_run_hard_stops(run)
                after = self._autonomy_row(lease["id"])["status"]
                if before != "halted" and after == "halted":
                    halted += 1
                    break
        shutdown = self._reconcile_full_shutdown_request()
        return {
            "expired": expired,
            "halted": halted,
            "emergency_forced": emergency_forced,
            "shutdown_dispatched": int(
                bool(shutdown and shutdown.get("status") == "dispatching")
            ),
        }

    def _activate_autonomy_lease(
        self,
        proposal: dict[str, Any],
        actor: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        lease_id = proposal.get("autonomy_lease_id")
        if not lease_id:
            raise ServiceError("Autonomy proposal is missing its lease id")
        if connection is None:
            lease = self._autonomy_row(str(lease_id))
        else:
            row = connection.execute(
                "SELECT * FROM autonomy_leases WHERE id = ?", (str(lease_id),)
            ).fetchone()
            if row is None:
                raise ServiceError(f"Unknown autonomy lease: {lease_id}")
            lease = dict(row)
        if lease["status"] != "pending":
            raise ServiceError(f"Autonomy lease status is {lease['status']}, not pending")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=float(lease["duration_hours"]))
        query = (
            "UPDATE autonomy_leases SET status = 'active', activated_at = ?, "
            "expires_at = ?, updated_at = ?, approved_by = ?, stopped_reason = NULL "
            "WHERE id = ? AND status = 'pending'"
        )
        params = (
            now.isoformat(timespec="seconds"),
            expires.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
            actor,
            lease_id,
        )
        if connection is None:
            changed = self.db.execute(query, params)
        else:
            changed = connection.execute(query, params).rowcount
        if changed != 1:
            raise ServiceError("Autonomy lease activation was resolved concurrently")
        self.db.event(
            "autonomy_lease_activated",
            actor,
            {
                "lease_id": lease_id,
                "expires_at": expires.isoformat(timespec="seconds"),
                "duration_hours": float(lease["duration_hours"]),
            },
            proposal["workflow_id"],
            connection=connection,
        )

    def _delegate_proposal_to_autonomy(
        self, proposal_id: str, lease_id: str, actor: str
    ) -> dict[str, Any]:
        lease = self._active_autonomy(lease_id)
        proposal = self.db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if proposal is None or proposal.get("autonomy_lease_id") != lease_id:
            raise ServiceError("Proposal is not bound to this autonomy lease")
        if proposal["workflow_id"] != lease["workflow_id"]:
            raise ServiceError("Proposal workflow does not match autonomy lease")
        if proposal["status"] != "pending":
            raise ServiceError("Only a pending proposal can receive delegated approval")
        approved_by = f"delegated autonomy lease {lease_id}"
        changed = self.db.execute(
            "UPDATE proposals SET status = 'approved', approved_at = ?, approved_by = ? "
            "WHERE id = ? AND status = 'pending'",
            (utc_now(), approved_by, proposal_id),
        )
        if changed != 1:
            raise ServiceError("Delegated approval was resolved concurrently")
        self.db.event(
            "proposal_delegated",
            actor,
            {
                "proposal_id": proposal_id,
                "kind": proposal["kind"],
                "autonomy_lease_id": lease_id,
            },
            proposal["workflow_id"],
        )
        return self.proposal(proposal_id)

    def _autonomy_row(self, lease_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM autonomy_leases WHERE id = ?", (lease_id,))
        if row is None:
            raise ServiceError(f"Unknown autonomy lease: {lease_id}")
        return row

    def _expire_autonomy_if_needed(self, row: dict[str, Any]) -> dict[str, Any]:
        if row["status"] == "pending":
            proposal = self.db.one(
                "SELECT status, expires_at FROM proposals WHERE id = ?",
                (row["proposal_id"],),
            )
            proposal_expired = False
            if proposal is not None:
                proposal_expired = proposal["status"] == "expired"
                if (
                    proposal["status"] == "pending"
                    and datetime.fromisoformat(str(proposal["expires_at"]))
                    <= datetime.now(timezone.utc)
                ):
                    self.db.execute(
                        "UPDATE proposals SET status = 'expired' WHERE id = ?",
                        (row["proposal_id"],),
                    )
                    proposal_expired = True
            if proposal_expired:
                now = utc_now()
                reason = "Approval proposal expired before lease activation."
                self.db.execute(
                    "UPDATE autonomy_leases SET status = 'expired', updated_at = ?, "
                    "stopped_reason = ? WHERE id = ?",
                    (now, reason, row["id"]),
                )
                self.db.execute(
                    "UPDATE measurement_sessions SET status = 'expired', "
                    "updated_at = ? WHERE autonomy_lease_id = ?",
                    (now, row["id"]),
                )
                self.db.event(
                    "autonomy_lease_expired",
                    "approval_expiry",
                    {
                        "lease_id": row["id"],
                        "proposal_id": row["proposal_id"],
                        "reason": reason,
                    },
                    row["workflow_id"],
                )
                row = self._autonomy_row(row["id"])

        expires_at = row.get("expires_at")
        if row["status"] in {"active", "paused"} and expires_at:
            if datetime.fromisoformat(str(expires_at)) <= datetime.now(timezone.utc):
                now = utc_now()
                self.db.execute(
                    "UPDATE autonomy_leases SET status = 'expired', updated_at = ?, "
                    "stopped_reason = ? WHERE id = ?",
                    (now, "Eight-hour authorization window expired.", row["id"]),
                )
                self.db.execute(
                    "UPDATE measurement_sessions SET status = 'expired', "
                    "updated_at = ? WHERE autonomy_lease_id = ?",
                    (now, row["id"]),
                )
                self.db.event(
                    "autonomy_lease_expired",
                    "autonomy_watchdog",
                    {"lease_id": row["id"]},
                    row["workflow_id"],
                )
                row = self._autonomy_row(row["id"])
        return row

    def _active_autonomy(
        self, lease_id: str, workflow_id: str | None = None
    ) -> dict[str, Any]:
        row = self._expire_autonomy_if_needed(self._autonomy_row(lease_id))
        if row["status"] != "active":
            raise ServiceError(
                f"Autonomy lease {lease_id} is {row['status']}; new actions are blocked"
            )
        if workflow_id is not None and row["workflow_id"] != workflow_id:
            raise ServiceError("Autonomy lease belongs to a different workflow")
        workflow = self._workflow(row["workflow_id"])
        if workflow["status"] != "active":
            raise ServiceError("Workflow is not active")
        return row

    def _validate_autonomy_scope(
        self, lease: dict[str, Any], *, node_id: str, targets: list[str]
    ) -> None:
        nodes = set(json_loads(lease["allowed_nodes_json"], []))
        allowed_targets = set(json_loads(lease["targets_json"], []))
        if node_id not in nodes:
            raise AutonomyScopeError(
                f"Node {node_id} is outside the autonomy lease; new human approval is required"
            )
        if not targets or not set(targets).issubset(allowed_targets):
            raise AutonomyScopeError(
                "Run targets are outside the autonomy lease; new human approval is required"
            )

    def _assert_autonomy_attempt_quota(
        self, lease: dict[str, Any], node_id: str, targets: list[str]
    ) -> None:
        max_attempts = int(lease["max_attempts_per_node_qubit"])
        counts = {target: 0 for target in targets}
        for run in self.db.all(
            "SELECT parameters_json FROM runs WHERE autonomy_lease_id = ? "
            "AND node_id = ?",
            (lease["id"], node_id),
        ):
            prior_targets = set(
                json_loads(run.get("parameters_json"), {}).get("qubits", [])
            )
            for target in counts:
                if target in prior_targets:
                    counts[target] += 1
        exhausted = {
            target: count for target, count in counts.items() if count >= max_attempts
        }
        if exhausted:
            raise AutonomyQuotaError(
                "Autonomy attempt limit reached for node/qubit pairs "
                f"{exhausted}; those targets are incomplete for this node. "
                "Schedule another qubit or advance the resolved targets; the "
                "autonomy lease remains active."
            )
        max_total = lease.get("max_total_runs")
        if max_total is not None:
            total = self.db.one(
                "SELECT COUNT(*) AS count FROM runs WHERE autonomy_lease_id = ?",
                (lease["id"],),
            )
            if int(total["count"]) >= int(max_total):
                raise AutonomyScopeError("Autonomy total-run limit reached")

    def _validate_autonomy_patch_targets(
        self, lease: dict[str, Any], patch: list[dict[str, Any]]
    ) -> None:
        allowed_targets = set(json_loads(lease["targets_json"], []))
        patch_targets = patch_qubit_targets(patch)
        if not patch_targets or not patch_targets.issubset(allowed_targets):
            raise AutonomyScopeError(
                "State patch targets are outside the autonomy lease; new approval is required"
            )

    def _assert_partial_pass_state_commit(
        self,
        run: dict[str, Any],
        patch: list[dict[str, Any]],
        allowed_statuses: set[str],
    ) -> None:
        """Allow a `needs_review` run to commit only its passing targets.

        Operator instruction 2026-09-20. Suppressing the whole patch lost
        calibrated values permanently: the node refuses a further run once every
        target is resolved, so a qubit that passed inside a `needs_review` run
        had no remaining way to obtain a pass-backed commit.
        """

        refusal = AutonomyEvidenceError(
            "Autonomous state commit requires a completed run with an allowed "
            f"analysis status: {sorted(allowed_statuses)}"
        )
        if not bool(
            self.policy.raw["autonomy"].get("partial_pass_state_commit", False)
        ):
            raise refusal
        if run["analysis_status"] != "needs_review":
            raise refusal
        analysis = json_loads(run.get("analysis_json"), {})
        if not isinstance(analysis, dict) or "passing_targets" not in analysis:
            raise AutonomyEvidenceError(
                "This run was analyzed before per-target evidence was recorded; "
                "re-analyze it before committing a partial-pass state patch"
            )
        passing = {str(name) for name in analysis.get("passing_targets", [])}
        patch_targets = patch_qubit_targets(patch)
        if not patch_targets:
            raise AutonomyEvidenceError(
                "A needs_review run may commit only per-qubit calibration values"
            )
        outside = sorted(patch_targets - passing)
        if outside:
            raise AutonomyEvidenceError(
                f"{outside} did not pass every check in this needs_review run; "
                f"only {sorted(passing)} may be committed from it"
            )

    def _validate_autonomous_scientific_state_commit(
        self,
        lease: dict[str, Any],
        run_id: str | None,
        patch: list[dict[str, Any]],
    ) -> None:
        if not run_id:
            raise AutonomyEvidenceError(
                "Autonomous scientific state commits require an accepted run"
            )
        run = self.db.one(
            "SELECT * FROM runs WHERE id = ? AND workflow_id = ?",
            (run_id, lease["workflow_id"]),
        )
        if run is None or run.get("autonomy_lease_id") != lease["id"]:
            raise AutonomyScopeError(
                "Accepted run is not bound to this autonomy lease"
            )
        self._validate_autonomy_scope(
            lease,
            node_id=run["node_id"],
            targets=json_loads(run["parameters_json"], {}).get("qubits", []),
        )
        allowed_statuses = set(
            json_loads(lease["auto_state_commit_statuses_json"], [])
        )
        if run["status"] != "completed":
            raise AutonomyEvidenceError(
                "Autonomous state commit requires a completed run"
            )
        if run["analysis_status"] not in allowed_statuses:
            self._assert_partial_pass_state_commit(run, patch, allowed_statuses)
        decision = self.db.one(
            "SELECT * FROM decisions WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        )
        if decision is None or decision["decision"] not in {"advance", "repeat"}:
            raise AutonomyEvidenceError(
                "Autonomous state commit requires a recorded advance/repeat decision"
            )
        if json_loads(decision["state_patch_json"], []) != patch:
            raise AutonomyEvidenceError(
                "State patch must exactly match the patch recorded in the decision"
            )
        self._validate_autonomy_patch_targets(lease, patch)

    def _set_autonomy_control(
        self,
        lease_id: str,
        client_id: str,
        reason: str,
        *,
        action: str,
    ) -> dict[str, Any]:
        if not client_id.strip() or not reason.strip():
            raise ServiceError("Autonomy control requires actor and reason")
        lease = self._expire_autonomy_if_needed(self._autonomy_row(lease_id))
        if action == "pause":
            if lease["status"] != "active":
                raise ServiceError("Only active autonomy can be paused")
            new_status = "paused"
        elif action in {"stop", "emergency_stop"}:
            if lease["status"] not in {"pending", "active", "paused"}:
                raise ServiceError(f"Autonomy lease is already {lease['status']}")
            new_status = "emergency_stopped" if action == "emergency_stop" else "revoked"
        else:
            raise ServiceError("Unknown autonomy control action")
        now = utc_now()
        self.db.execute(
            "UPDATE autonomy_leases SET status = ?, stopped_reason = ?, updated_at = ? "
            "WHERE id = ?",
            (new_status, reason.strip(), now, lease_id),
        )
        self.db.execute(
            "UPDATE measurement_sessions SET status = ?, updated_at = ? "
            "WHERE autonomy_lease_id = ?",
            (new_status, now, lease_id),
        )
        self.db.execute(
            "UPDATE proposals SET status = 'cancelled' WHERE autonomy_lease_id = ? "
            "AND status IN ('pending', 'approved') AND uses = 0",
            (lease_id,),
        )
        active_run = self.db.one(
            "SELECT * FROM runs WHERE autonomy_lease_id = ? "
            "AND status IN ('starting', 'running', 'stopping') "
            "ORDER BY started_at DESC LIMIT 1",
            (lease_id,),
        )
        stop_result = None
        if action in {"stop", "emergency_stop"} and active_run is not None:
            stop_result = self.runner.stop(active_run["id"], client_id)
            if action == "emergency_stop":
                self._schedule_force_stop(active_run["id"], client_id)
        self.db.event(
            f"autonomy_{action}",
            client_id,
            {"lease_id": lease_id, "reason": reason.strip(), "run": stop_result},
            lease["workflow_id"],
        )
        result = self.autonomy_status(lease_id=lease_id)
        result["active_run_control"] = stop_result
        return result

    def _schedule_force_stop(self, run_id: str, actor: str) -> None:
        grace = int(
            self.policy.raw["autonomy"].get("emergency_stop_grace_seconds", 30)
        )
        run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if run is None or not run.get("autonomy_lease_id"):
            raise ServiceError("Emergency stop run is not bound to autonomy")
        now = datetime.now(timezone.utc)
        force_after = now + timedelta(seconds=max(0, grace))
        self.db.execute(
            """
            INSERT OR REPLACE INTO emergency_stops(
                run_id, autonomy_lease_id, requested_at, force_after, actor, status
            ) VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (
                run_id,
                run["autonomy_lease_id"],
                now.isoformat(timespec="seconds"),
                force_after.isoformat(timespec="seconds"),
                actor,
            ),
        )

        def force_after_grace() -> None:
            time.sleep(max(0, grace))
            self._force_pending_emergency_stop(run_id)

        threading.Thread(
            target=force_after_grace,
            name=f"jy-emergency-stop-{run_id[:8]}",
            daemon=True,
        ).start()

    def _enforce_pending_emergency_stops(self) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = self.db.all(
            "SELECT run_id FROM emergency_stops "
            "WHERE status = 'pending' AND force_after <= ?",
            (now,),
        )
        return sum(
            1 for row in rows if self._force_pending_emergency_stop(row["run_id"])
        )

    def _force_pending_emergency_stop(self, run_id: str) -> bool:
        pending = self.db.one(
            "SELECT * FROM emergency_stops WHERE run_id = ? AND status = 'pending'",
            (run_id,),
        )
        if pending is None:
            return False
        run = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        try:
            if run and run["status"] in {"starting", "running", "stopping"}:
                result = self.runner.force_stop(run_id, pending["actor"])
            else:
                result = {
                    "run_id": run_id,
                    "forced": False,
                    "status": run["status"] if run else "missing",
                }
            status = "completed"
        except Exception as exc:
            result = {"run_id": run_id, "error": str(exc)}
            status = "failed"
            if run:
                self.db.event(
                    "emergency_force_stop_failed",
                    pending["actor"],
                    result,
                    run["workflow_id"],
                )
        self.db.execute(
            "UPDATE emergency_stops SET status = ?, completed_at = ?, result_json = ? "
            "WHERE run_id = ?",
            (status, utc_now(), json_dumps(result), run_id),
        )
        return bool(result.get("forced"))

    def _halt_autonomy(
        self,
        lease_id: str,
        reason: str,
        actor: str,
        *,
        stop_active: bool,
    ) -> dict[str, Any]:
        lease = self._autonomy_row(lease_id)
        if lease["status"] not in {"pending", "active", "paused"}:
            return self._decode_autonomy(lease)
        now = utc_now()
        self.db.execute(
            "UPDATE autonomy_leases SET status = 'halted', stopped_reason = ?, "
            "updated_at = ? WHERE id = ?",
            (reason, now, lease_id),
        )
        self.db.execute(
            "UPDATE measurement_sessions SET status = 'halted', updated_at = ? "
            "WHERE autonomy_lease_id = ?",
            (now, lease_id),
        )
        self.db.execute(
            "UPDATE proposals SET status = 'cancelled' WHERE autonomy_lease_id = ? "
            "AND status IN ('pending', 'approved') AND uses = 0",
            (lease_id,),
        )
        active_run = self.db.one(
            "SELECT id FROM runs WHERE autonomy_lease_id = ? "
            "AND status IN ('starting', 'running', 'stopping') LIMIT 1",
            (lease_id,),
        )
        if stop_active and active_run is not None:
            try:
                self.runner.stop(active_run["id"], actor)
            except Exception:
                pass
        self.db.event(
            "autonomy_halted",
            actor,
            {"lease_id": lease_id, "reason": reason},
            lease["workflow_id"],
        )
        return self.autonomy_status(lease_id=lease_id)

    @staticmethod
    def _is_recoverable_scheduling_block(exc: BaseException) -> bool:
        """True when the refusal happened before the worker reached hardware.

        Operator instruction 2026-09-20: this used to compare three exact
        message strings, so rewording any one of them silently turned a
        recoverable refusal into a lease-ending halt. Scope, quota, and policy
        refusals are now recognized by type, and the remaining message tests
        only cover the paused/stopped-workflow wording.
        """

        if isinstance(
            exc, (AutonomyScopeError, AutonomyQuotaError, PolicyError)
        ):
            return True
        message = str(exc)
        return (
            "; new actions are blocked" in message
            or message == "Workflow is not active"
            or message.startswith("Resume measurement mode before")
        )

    @staticmethod
    def _is_instrument_connectivity_failure(run: dict[str, Any]) -> bool:
        if str(run.get("termination_cause") or "") == "instrument_unreachable":
            return True
        analysis = json_loads(run.get("analysis_json"), {})
        return analysis.get("failure_category") == "instrument_unreachable"

    @staticmethod
    def _program_submission_timeout(run: dict[str, Any]) -> dict[str, Any] | None:
        """Return the analysis of a probe-confirmed submission timeout.

        The worker records this category only after the QOP answered a health
        check, so it is a program-size failure and not an outage.
        """

        analysis = json_loads(run.get("analysis_json"), {})
        cause = str(run.get("termination_cause") or "")
        if (
            cause == "program_submission_timeout"
            or analysis.get("failure_category") == "program_submission_timeout"
        ):
            return analysis if isinstance(analysis, dict) else {}
        return None

    def _pause_already_resumed(
        self, run: dict[str, Any], event_type: str
    ) -> bool:
        """Skip re-pausing a failure the operator has already resumed past.

        The watchdog re-examines the same failed run on every sweep, so
        without this the resume is undone in the same second it is granted and
        `恢復量測` can never take effect.
        """

        pause = self.db.one(
            "SELECT created_at FROM events "
            "WHERE workflow_id = ? AND event_type = ? "
            "AND json_extract(payload_json, '$.run_id') = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (run["workflow_id"], event_type, run["id"]),
        )
        if pause is None:
            return False
        resume = self.db.one(
            "SELECT created_at FROM events "
            "WHERE workflow_id = ? "
            "AND event_type IN ('measurement_mode_resumed', 'autonomy_resumed') "
            "AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (run["workflow_id"], pause["created_at"]),
        )
        return resume is not None

    def _instrument_outage_already_resumed(self, run: dict[str, Any]) -> bool:
        """Skip re-pausing an outage the operator has already resumed past."""

        return self._pause_already_resumed(
            run, "measurement_paused_for_instrument_error"
        )

    def _pause_for_instrument_connectivity(
        self, run: dict[str, Any]
    ) -> None:
        """Idempotently pause scheduling after a recognized instrument outage."""

        if not self._is_instrument_connectivity_failure(run):
            return
        if self._instrument_outage_already_resumed(run):
            return
        self._pause_autonomy_for_review(
            run,
            f"Instrument connectivity error paused run {run['id']}; no new "
            "experiment will be scheduled until the operator resumes.",
            "measurement_paused_for_instrument_error",
            "instrument_failure_guard",
        )

    def _resolve_autonomy_duration(self, requested: float | None) -> float:
        """Clamp-check an entry-supplied lease budget against policy.

        Operator instruction 2026-09-20: automatic-mode entry may name the hours
        for that session instead of always taking the configured default. The
        human still approves the number shown on the Dashboard.
        """

        config = self.policy.raw["autonomy"]
        default_hours = float(config["duration_hours"])
        if requested is None:
            return default_hours
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise ServiceError("duration_hours must be a number of hours")
        hours = float(requested)
        maximum = float(config.get("max_duration_hours", default_hours))
        if not math.isfinite(hours) or hours <= 0:
            raise ServiceError("duration_hours must be a positive number of hours")
        if hours > maximum:
            raise ServiceError(
                f"duration_hours {hours:g} exceeds the policy maximum of "
                f"{maximum:g} hours"
            )
        return hours

    def _record_refused_autonomy_action(
        self,
        lease_id: str,
        workflow_id: str,
        category: str,
        detail: str,
    ) -> None:
        """Audit an in-scope action that was refused before it reached hardware.

        Operator instruction 2026-09-20. Refusing the action is the protection;
        ending the lease on top of that only costs a new human approval and
        stops a bring-up that could have continued. The refusal stays visible on
        the Dashboard and in the audit log.
        """

        self.db.event(
            "autonomy_action_refused",
            "autonomy_guard",
            {"lease_id": lease_id, "category": category, "detail": detail},
            workflow_id,
        )

    def recoverable_worker_exception(
        self, run: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Match a worker crash against the registered recoverable signatures.

        Operator instruction 2026-09-20: a crash that is a parameter the
        protected node itself rejects is deterministic and correctable, so it
        leaves the lease usable. Everything unrecognized pauses for a human.
        """

        traceback_text = str(run.get("error") or "")
        if not traceback_text:
            return None
        node_id = str(run.get("node_id") or "")
        parameters = json_loads(run.get("parameters_json"), {})
        parameter_names = set(parameters) if isinstance(parameters, dict) else set()
        folded = traceback_text.casefold()
        signatures = self.policy.raw["autonomy"].get(
            "recoverable_worker_exceptions", []
        )
        for signature in signatures if isinstance(signatures, list) else []:
            if not isinstance(signature, dict):
                continue
            nodes = signature.get("nodes") or []
            if nodes and node_id not in {str(item) for item in nodes}:
                continue
            types = [str(item).casefold() for item in signature.get("exception_types", [])]
            if types and not any(f"{item}:" in folded or f"{item} " in folded for item in types):
                continue
            markers = [str(item).casefold() for item in signature.get("message_markers", [])]
            if markers and not any(marker in folded for marker in markers):
                continue
            required = {str(item) for item in signature.get("parameters", [])}
            if required and not (required & parameter_names):
                continue
            return {
                "id": str(signature.get("id") or "unnamed_signature"),
                "remedy": str(signature.get("remedy") or "").strip(),
                "parameters": sorted(required & parameter_names),
            }
        return None

    def _pause_autonomy_for_review(
        self,
        run: dict[str, Any],
        reason: str,
        event_type: str,
        actor: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Pause the lease and scheduling without ending the authorization.

        The operator inspects the Dashboard and resumes the same lease with
        `恢復量測`; no new human approval is needed.
        """

        now = utc_now()
        with self.db.transaction(immediate=True) as connection:
            changed = 0
            lease_id = run.get("autonomy_lease_id")
            if lease_id:
                changed += connection.execute(
                    "UPDATE autonomy_leases SET status = 'paused', stopped_reason = ?, "
                    "updated_at = ? WHERE id = ? AND status = 'active'",
                    (reason, now, lease_id),
                ).rowcount
            else:
                changed += connection.execute(
                    "UPDATE workflows SET status = 'paused', updated_at = ? "
                    "WHERE id = ? AND status = 'active'",
                    (now, run["workflow_id"]),
                ).rowcount
            changed += connection.execute(
                "UPDATE measurement_sessions SET status = 'paused', updated_at = ? "
                "WHERE workflow_id = ? AND status IN ('active', 'halted')",
                (now, run["workflow_id"]),
            ).rowcount
            if changed:
                self.db.event(
                    event_type,
                    actor,
                    {
                        "run_id": run["id"],
                        "reason": reason,
                        "hardware_lock_retained": self.settings.lock_path.exists(),
                        **(detail or {}),
                    },
                    run["workflow_id"],
                    connection=connection,
                )

    def _enforce_run_hard_stops(self, run: dict[str, Any]) -> None:
        lease_id = run.get("autonomy_lease_id")
        if not lease_id:
            return
        lease = self._autonomy_row(str(lease_id))
        if lease["status"] not in {"active", "paused"}:
            return
        if run["status"] in {"stopped", "cancelled_by_shutdown"}:
            return
        if run["status"] in {"failed", "force_stopped"}:
            if self._is_instrument_connectivity_failure(run):
                self._pause_for_instrument_connectivity(run)
                return
            submission = self._program_submission_timeout(run)
            if submission is not None:
                # A live probe answered after the timeout, so the instrument is
                # healthy and the program was simply too large to submit. The
                # remedy is a smaller multiplex group, which the agent can do
                # without an operator, so the lease stays active. The worker
                # already paused if a single target had left no room to halve.
                if not submission.get("measurement_paused"):
                    self.db.event(
                        "autonomy_program_submission_timeout",
                        "autonomy_watchdog",
                        {
                            "run_id": run["id"],
                            "attempted_target_count": submission.get(
                                "attempted_target_count"
                            ),
                            "instrument_probe": submission.get(
                                "instrument_probe"
                            ),
                            "remedy": (
                                "Halve the multiplex group and repeat the node."
                            ),
                        },
                        run["workflow_id"],
                    )
                return
            recoverable = self.recoverable_worker_exception(run)
            if recoverable is not None:
                # Registered node-parameter rejection: nothing ran, the cause is
                # known, and the remedy is a different parameter. Keep the lease.
                self.db.event(
                    "autonomy_recoverable_worker_exception",
                    "autonomy_watchdog",
                    {
                        "run_id": run["id"],
                        "signature": recoverable["id"],
                        "parameters": recoverable["parameters"],
                        "remedy": recoverable["remedy"],
                    },
                    run["workflow_id"],
                )
                return
            if self._pause_already_resumed(
                run, "autonomy_paused_for_worker_failure"
            ):
                # The operator has seen this failure and resumed past it.
                # Re-pausing here would undo the resume on the next sweep and
                # leave the lease permanently stuck.
                return
            self._pause_autonomy_for_review(
                run,
                f"Unrecognized worker failure for run {run['id']} paused "
                "scheduling for operator review: "
                f"{run.get('error') or run['status']}",
                "autonomy_paused_for_worker_failure",
                "autonomy_watchdog",
                {"termination_cause": run.get("termination_cause")},
            )
            return
        if run["status"] == "completed":
            snapshot_path = run.get("snapshot_path")
            if (
                run.get("snapshot_id") is None
                or not snapshot_path
                or not Path(str(snapshot_path)).is_dir()
            ):
                self._halt_autonomy(
                    str(lease_id),
                    f"Snapshot is missing for completed run {run['id']}",
                    "autonomy_watchdog",
                    stop_active=False,
                )
            return
        if run["status"] in {"starting", "running", "stopping"}:
            pid = run.get("pid")
            if isinstance(pid, int) and not self.runner.process_is_alive(pid):
                evidence = self.runner.exit_evidence(str(run["id"]))
                stop_intent = str(
                    run.get("stop_intent")
                    or evidence.get("stop_request", {}).get("intent")
                    or ""
                )
                if stop_intent:
                    terminal_status = (
                        "cancelled_by_shutdown"
                        if stop_intent == "full_shutdown"
                        else "stopped"
                    )
                    cause = str(
                        evidence.get("exit_receipt", {}).get("termination_cause")
                        or "intentional_worker_exit"
                    )
                    self.db.execute(
                        "UPDATE runs SET status = ?, finished_at = ?, "
                        "termination_cause = ? WHERE id = ?",
                        (terminal_status, utc_now(), cause, run["id"]),
                    )
                    self.db.event(
                        "run_cancelled_by_shutdown"
                        if terminal_status == "cancelled_by_shutdown"
                        else "run_stopped",
                        "autonomy_watchdog",
                        {
                            "run_id": run["id"],
                            "intent": stop_intent,
                            "exit_evidence": evidence,
                            "hardware_lock_retained": self.settings.lock_path.exists(),
                        },
                        run["workflow_id"],
                    )
                    return
                message = (
                    f"Worker process {pid} exited without recording completion for "
                    f"run {run['id']}; hardware lock retained for inspection."
                )
                self.db.execute(
                    "UPDATE runs SET status = 'failed', finished_at = ?, error = ? "
                    "WHERE id = ?",
                    (utc_now(), message, run["id"]),
                )
                self._halt_autonomy(
                    str(lease_id),
                    message,
                    "autonomy_watchdog",
                    stop_active=False,
                )
                return
            try:
                lock = json.loads(
                    self.settings.lock_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                self._halt_autonomy(
                    str(lease_id),
                    f"Hardware lock is missing or unreadable for run {run['id']}: {exc}",
                    "autonomy_watchdog",
                    stop_active=True,
                )
                return
            if lock.get("run_id") != run["id"]:
                self._halt_autonomy(
                    str(lease_id),
                    f"Hardware lock owner does not match run {run['id']}",
                    "autonomy_watchdog",
                    stop_active=True,
                )

    def _decode_autonomy(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["targets"] = json_loads(result.pop("targets_json"), [])
        result["allowed_nodes"] = json_loads(result.pop("allowed_nodes_json"), [])
        result["auto_state_commit_analysis_statuses"] = json_loads(
            result.pop("auto_state_commit_statuses_json"), []
        )
        result["halt_conditions"] = json_loads(
            result.pop("halt_conditions_json"), []
        )
        attempts: dict[str, dict[str, int]] = {}
        for run in self.db.all(
            "SELECT node_id, parameters_json FROM runs WHERE autonomy_lease_id = ?",
            (result["id"],),
        ):
            node_counts = attempts.setdefault(run["node_id"], {})
            for target in json_loads(run["parameters_json"], {}).get("qubits", []):
                node_counts[target] = node_counts.get(target, 0) + 1
        result["attempts_by_node_qubit"] = attempts
        quota_reached: dict[str, list[str]] = {}
        incomplete: dict[str, list[str]] = {}
        resolved_by_node: dict[str, list[str]] = {}
        scientifically_unmeasurable = (
            self._scientifically_unmeasurable_targets_by_node(
                result["workflow_id"], lease_id=result["id"]
            )
        )
        max_attempts = int(result["max_attempts_per_node_qubit"])
        for node_id in set(attempts) | set(scientifically_unmeasurable):
            node_counts = attempts.get(node_id, {})
            reached = {
                target
                for target, count in node_counts.items()
                if count >= max_attempts
            }
            resolved = self._node_target_resolution(
                result["workflow_id"], node_id
            )
            if resolved:
                resolved_by_node[node_id] = sorted(resolved)
            if reached:
                quota_reached[node_id] = sorted(reached)
            unresolved = (
                reached | scientifically_unmeasurable.get(node_id, set())
            ) - resolved
            if unresolved:
                incomplete[node_id] = sorted(unresolved)
        result["resolved_targets_by_node"] = resolved_by_node
        result["quota_reached_by_node"] = quota_reached
        result["scientifically_unmeasurable_targets_by_node"] = {
            node_id: sorted(targets)
            for node_id, targets in scientifically_unmeasurable.items()
            if targets
        }
        result["incomplete_targets_by_node"] = incomplete
        result["total_runs"] = sum(
            1
            for _ in self.db.all(
                "SELECT id FROM runs WHERE autonomy_lease_id = ?", (result["id"],)
            )
        )
        result["control_url"] = self.autonomy_control_url(result["id"])
        result["manual_controls"] = {
            "pause": "Finish the active run but schedule nothing new.",
            "stop": "Revoke the lease and gracefully stop the active worker.",
            "emergency_stop": (
                "Revoke, request graceful stop, then terminate the exact worker "
                "after the grace period while retaining the hardware lock."
            ),
        }
        result["continuation_policy"] = {
            "mode": "continue_until_workflow_complete",
            "continue_after_needs_review": True,
            "continue_other_resolved_qubits_after_quota": True,
            "terminal_conditions": [
                "scientifically_unmeasurable_target",
                "per_node_qubit_attempt_limit",
                "authorization_time_limit",
                "workflow_complete",
                "operator_stop",
                *result["halt_conditions"],
            ],
        }
        return result

    def _latest_review_run_id(
        self, workflow_id: str, proposed_node: str
    ) -> str | None:
        if not proposed_node:
            return None
        row = self.db.one(
            """
            SELECT d.run_id
            FROM decisions d
            JOIN runs r ON r.id = d.run_id
            WHERE d.workflow_id = ?
              AND (d.next_node = ? OR (r.node_id = ? AND d.decision IN ('repeat', 'manual_review')))
            ORDER BY d.created_at DESC
            LIMIT 1
            """,
            (workflow_id, proposed_node, proposed_node),
        )
        return str(row["run_id"]) if row is not None else None

    def _latest_workflow_decision_run_id(self, workflow_id: str) -> str | None:
        row = self.db.one(
            "SELECT run_id FROM decisions WHERE workflow_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (workflow_id,),
        )
        return str(row["run_id"]) if row is not None else None

    def _approved_proposal(self, proposal_id: str, kind: str) -> dict[str, Any]:
        proposal = self.db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if proposal is None:
            raise ServiceError(f"Unknown proposal: {proposal_id}")
        if proposal["kind"] != kind or proposal["status"] != "approved":
            raise ServiceError(f"An approved {kind} proposal is required")
        if proposal["uses"] >= proposal["max_uses"]:
            raise ServiceError("Proposal has already been consumed")
        if datetime.fromisoformat(proposal["expires_at"]) <= datetime.now(timezone.utc):
            raise ServiceError("Proposal has expired")
        return proposal

    def _02c_target_resolution(self, workflow_id: str) -> tuple[set[str], set[str]]:
        rows = self.db.all(
            "SELECT analysis_status, analysis_json FROM runs "
            "WHERE workflow_id = ? AND node_id = '02c'",
            (workflow_id,),
        )
        return resolve_02c_targets(rows)

    def _node_target_resolution(
        self, workflow_id: str, node_id: str
    ) -> set[str]:
        rows = self.db.all(
            "SELECT r.status, r.analysis_status, r.analysis_json, "
            "r.parameters_json, d.decision FROM runs r "
            "LEFT JOIN decisions d ON d.run_id = r.id "
            "WHERE r.workflow_id = ? AND r.node_id = ? ORDER BY r.rowid",
            (workflow_id, node_id),
        )
        resolved = self._resolve_targets_from_rows(node_id, rows)
        if node_id in SUBGROUP_NODES:
            return resolved
        for row in rows:
            if (
                row.get("status") == "completed"
                and row.get("analysis_status") == "pass"
            ):
                parameters = json_loads(row.get("parameters_json"), {})
                names = parameters.get("qubits", [])
                if isinstance(names, list):
                    resolved.update(str(name) for name in names)
        return resolved

    def _resolve_targets_from_rows(
        self, node_id: str, rows: list[dict[str, Any]]
    ) -> set[str]:
        analysis_rules = self.policy.raw["analysis"]
        rules_03a = analysis_rules["03a"]
        return resolve_node_targets(
            node_id,
            rows,
            float(rules_03a["min_robust_snr"]),
            float(analysis_rules["04"]["min_robust_snr"]),
            float(analysis_rules["05"]["min_robust_snr"]),
            1e6 * float(rules_03a["final_min_feature_fwhm_mhz"]),
            1e6 * float(rules_03a["final_max_feature_fwhm_mhz"]),
            float(analysis_rules.get("02x", {}).get("min_robust_snr", 3.0)),
            float(analysis_rules.get("02a", {}).get("min_robust_snr", 3.0)),
        )

    def _autonomy_incomplete_targets_for_node(
        self, workflow_id: str, node_id: str
    ) -> set[str]:
        lease = self.db.one(
            "SELECT * FROM autonomy_leases WHERE workflow_id = ? "
            "AND status IN ('active', 'paused') ORDER BY created_at DESC LIMIT 1",
            (workflow_id,),
        )
        if lease is None:
            return set()
        max_attempts = int(lease["max_attempts_per_node_qubit"])
        counts: dict[str, int] = {}
        for run in self.db.all(
            "SELECT parameters_json FROM runs WHERE autonomy_lease_id = ? "
            "AND node_id = ?",
            (lease["id"], node_id),
        ):
            for target in set(
                json_loads(run.get("parameters_json"), {}).get("qubits", [])
            ):
                counts[str(target)] = counts.get(str(target), 0) + 1
        quota_reached = {
            target for target, count in counts.items() if count >= max_attempts
        }
        scientifically_unmeasurable = (
            self._scientifically_unmeasurable_targets_by_node(
                workflow_id, lease_id=str(lease["id"])
            ).get(node_id, set())
        )
        return (
            quota_reached | scientifically_unmeasurable
        ) - self._node_target_resolution(workflow_id, node_id)

    def _scientifically_unmeasurable_targets_by_node(
        self, workflow_id: str, *, lease_id: str | None = None
    ) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for row in self.db.all(
            "SELECT payload_json FROM events WHERE workflow_id = ? "
            "AND event_type = 'autonomy_targets_scientifically_unmeasurable' "
            "ORDER BY id",
            (workflow_id,),
        ):
            payload = json_loads(row.get("payload_json"), {})
            if lease_id is not None and payload.get("lease_id") != lease_id:
                continue
            node_id = str(payload.get("node_id", ""))
            targets = payload.get("targets", [])
            if not node_id or not isinstance(targets, list):
                continue
            result.setdefault(node_id, set()).update(str(item) for item in targets)
        return result

    def _03a_candidate_targets(self, workflow_id: str) -> set[str]:
        rows = self.db.all(
            "SELECT r.status, r.analysis_status, r.analysis_json, "
            "r.parameters_json, d.decision FROM runs r "
            "LEFT JOIN decisions d ON d.run_id = r.id "
            "WHERE r.workflow_id = ? AND r.node_id = '03a' ORDER BY r.rowid",
            (workflow_id,),
        )
        rules = self.policy.raw["analysis"]["03a"]
        return resolve_03a_candidate_targets(
            rows, float(rules["min_robust_snr"])
        )

    def _03a_shift_ready_targets(self, workflow_id: str) -> set[str]:
        rows = self.db.all(
            "SELECT r.status, r.analysis_status, r.analysis_json, "
            "r.parameters_json, d.decision FROM runs r "
            "LEFT JOIN decisions d ON d.run_id = r.id "
            "WHERE r.workflow_id = ? AND r.node_id = '03a' ORDER BY r.rowid",
            (workflow_id,),
        )
        rules = self.policy.raw["analysis"]["03a"]
        return resolve_03a_shift_ready_targets(
            rows,
            min_snr=float(rules["min_robust_snr"]),
            min_edge_fraction=float(rules["min_edge_fraction"]),
            max_noise_confirmation_averages=int(
                rules["max_noise_confirmation_num_averages"]
            ),
        )

    def _node_has_completed_run(self, workflow_id: str, node_id: str) -> bool:
        row = self.db.one(
            "SELECT id FROM runs WHERE workflow_id = ? AND node_id = ? "
            "AND status = 'completed' LIMIT 1",
            (workflow_id, node_id),
        )
        return row is not None

    def _validate_shared_parameter_first_batch(
        self,
        workflow: dict[str, Any],
        node_id: str,
        requested: set[str],
    ) -> None:
        if node_id not in SHARED_FIRST_BATCH_NODES:
            return
        cap = self._node_multiplex_cap(node_id)
        if self._node_has_completed_run(workflow["id"], node_id):
            return
        active = self._active_targets_for_node(workflow, node_id)
        if cap is not None and len(active) > cap:
            # A capped node has no full-width batch to demand; the cap itself
            # is the shared-parameter unit.
            return
        if requested != active:
            if self._node_full_width_submission_failed(
                workflow["id"], node_id, active
            ):
                return
            raise AutonomyScopeError(
                f"The first {node_id} run must multiplex every active target "
                f"{sorted(active)} with the same parameters"
            )

    def _node_multiplex_cap(self, node_id: str) -> int | None:
        """Largest multiplex group this node allows, if it caps one.

        Operator instruction 2026-09-21: 04 never submits more than five
        targets at once on this hardware. Nodes without the key are uncapped
        and keep multiplexing every active target.
        """

        try:
            definition = self.policy.node_definition(node_id)
        except Exception:  # pragma: no cover - unknown node handled elsewhere
            return None
        value = definition.get("max_multiplex_targets")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value > 0 else None

    def _node_is_single_pass(self, node_id: str) -> bool:
        """True for a node that runs once on defaults and then moves on.

        Operator instruction 2026-09-21 for 07d and 07b: one full-width run
        with the node defaults, no retry, no parameter adjustment and no
        per-target chasing. Passing evidence still commits, because that is
        the node's output; a target that does not pass simply carries no
        update and does not hold the workflow up.
        """

        try:
            definition = self.policy.node_definition(node_id)
        except Exception:  # pragma: no cover - unknown node handled elsewhere
            return False
        return bool(definition.get("single_pass"))

    def _validate_single_pass_node(
        self, workflow: dict[str, Any], node_id: str, merged: dict[str, Any]
    ) -> None:
        """Refuse a second run at a node that runs once on defaults.

        The one exception is the active-reset repeat the operator asked for at
        07b: a qubit whose thermal run cleared the trigger fidelity earns
        exactly one more 07b run with active reset, and that repeat is its
        accepted result.
        """

        if not self._node_is_single_pass(node_id):
            return
        if not self._node_has_completed_run(workflow["id"], node_id):
            return
        if self._is_active_reset_repeat(workflow, node_id, merged):
            return
        # Single pass means one pass over each target, not one run over the
        # whole chip. A node that has to be split into multiplex subgroups
        # still needs a run per subgroup, so only refuse targets that have
        # already been measured here.
        measured = self._single_pass_measured_targets(workflow, node_id)
        requested = {str(name) for name in merged.get("qubits", [])}
        repeated = sorted(requested & measured)
        if not repeated:
            return
        raise AutonomyScopeError(
            f"{node_id} runs once per target on the node defaults and then "
            f"advances; {repeated} already have a completed run here, so do "
            "not schedule another for them"
        )

    def _single_pass_measured_targets(
        self, workflow: dict[str, Any], node_id: str
    ) -> set[str]:
        """Targets already measured by a completed run at this node."""

        measured: set[str] = set()
        for run in self.db.all(
            "SELECT parameters_json FROM runs WHERE workflow_id = ? "
            "AND node_id = ? AND status = 'completed'",
            (workflow["id"], node_id),
        ):
            names = json_loads(run.get("parameters_json"), {}).get("qubits", [])
            if isinstance(names, list):
                measured.update(str(name) for name in names)
        return measured

    def _is_active_reset_repeat(
        self, workflow: dict[str, Any], node_id: str, merged: dict[str, Any]
    ) -> bool:
        """True for the single permitted active-reset repeat at 07b."""

        if node_id != str(
            self.policy.raw["reset_policy"]["active_qualification_node"]
        ):
            return False
        if _reset_type(merged) != "active":
            return False
        if self._active_reset_repeat_targets(workflow) is None:
            return False
        for run in self.db.all(
            "SELECT parameters_json FROM runs WHERE workflow_id = ? "
            "AND node_id = ?",
            (workflow["id"], node_id),
        ):
            if _reset_type(json_loads(run.get("parameters_json"), {})) == "active":
                # The repeat has already been taken.
                return False
        return True

    def _active_reset_repeat_targets(
        self, workflow: dict[str, Any]
    ) -> set[str] | None:
        """Targets whose thermal 07b fidelity earns the active-reset repeat.

        Returns None when no completed thermal 07b evidence exists yet, and an
        empty set when it exists but nothing cleared the trigger.
        """

        node_id = str(self.policy.raw["reset_policy"]["active_qualification_node"])
        rules = self.policy.raw["analysis"].get(node_id, {})
        trigger = rules.get("active_reset_trigger_fidelity")
        if trigger is None:
            trigger = rules.get("active_reset_min_readout_fidelity")
        if trigger is None:
            return None
        trigger = float(trigger)
        seen = False
        qualifying: set[str] = set()
        for run in self.db.all(
            "SELECT parameters_json, analysis_json FROM runs "
            "WHERE workflow_id = ? AND node_id = ? AND status = 'completed' "
            "ORDER BY rowid",
            (workflow["id"], node_id),
        ):
            parameters = json_loads(run.get("parameters_json"), {})
            if _reset_type(parameters) == "active":
                continue
            analysis = json_loads(run.get("analysis_json"), {})
            results = analysis.get("fit_quality", {})
            results = results.get("results") if isinstance(results, dict) else None
            if not isinstance(results, dict):
                continue
            seen = True
            for name, fit in results.items():
                if not isinstance(fit, dict):
                    continue
                fidelity = fit.get("readout_fidelity")
                if not isinstance(fidelity, (int, float)) or isinstance(
                    fidelity, bool
                ):
                    continue
                if float(fidelity) > trigger:
                    qualifying.add(str(name))
        return qualifying if seen else None

    def _validate_multiplex_cap(self, node_id: str, requested: set[str]) -> None:
        cap = self._node_multiplex_cap(node_id)
        if cap is None or len(requested) <= cap:
            return
        raise AutonomyScopeError(
            f"{node_id} multiplexes at most {cap} targets per run; "
            f"{len(requested)} were requested. Split them into groups of "
            f"{cap} or fewer."
        )

    def _node_full_width_submission_failed(
        self,
        workflow_id: str,
        node_id: str,
        active: set[str],
    ) -> bool:
        """True once a full-width attempt at this node failed to reach hardware.

        Operator instruction 2026-09-19: a program covering every active target
        can be too large to finish compiling and transferring before the QOP
        queue-submission deadline.  Such a failure is recorded as
        ``program_submission_timeout`` once a live probe has confirmed the
        instrument is healthy, and older runs recorded it as
        ``instrument_unreachable``; both are accepted here.  The shared-first-
        batch rule would otherwise block the only remaining recovery, measuring
        the targets in smaller multiplex subgroups, so it is waived once the
        full width has demonstrably been tried and could not be submitted.
        """

        for run in self.db.all(
            "SELECT parameters_json FROM runs WHERE workflow_id = ? "
            "AND node_id = ? AND status = 'failed' "
            "AND termination_cause IN "
            "('program_submission_timeout', 'instrument_unreachable')",
            (workflow_id, node_id),
        ):
            attempted = {
                str(target)
                for target in json_loads(run.get("parameters_json"), {}).get(
                    "qubits", []
                )
            }
            if attempted == active:
                return True
        return False

    def _node_is_finished_and_fully_decided(
        self, workflow: dict[str, Any], node_id: str
    ) -> bool:
        """True when the current node is done but cannot record `advance`.

        The playbook's scientific boundary requires `manual_review` on the
        target's evidence run. When that boundary lands on the node's last run
        there is nothing left to record `advance` on, and no further run can be
        scheduled either, because every active target is resolved or
        incomplete. Without this the workflow deadlocks at a node it has
        actually finished. Requesting the next node in sequence is then the
        advance, and it is allowed only when the node really is finished and
        every one of its runs already carries a decision.
        """

        current = str(workflow["current_node"] or "")
        if not current or current not in SUBGROUP_NODES:
            return False
        sequence = list(self.settings.workflow_sequence)
        if current not in sequence:
            return False
        index = sequence.index(current)
        if index >= len(sequence) - 1 or node_id != sequence[index + 1]:
            return False
        if not self._node_has_completed_run(workflow["id"], current):
            return False
        if self._node_is_single_pass(current):
            # Finished once every active target has had its one pass.
            unmeasured = self._active_targets_for_node(
                workflow, current
            ) - self._single_pass_measured_targets(workflow, current)
            if unmeasured:
                return False
        else:
            active = self._active_targets_for_node(workflow, current)
            unresolved = (
                active
                - self._node_target_resolution(workflow["id"], current)
                - self._autonomy_incomplete_targets_for_node(workflow["id"], current)
            )
            if unresolved:
                return False
        undecided = self.db.one(
            "SELECT r.id FROM runs r LEFT JOIN decisions d ON d.run_id = r.id "
            "WHERE r.workflow_id = ? AND r.node_id = ? AND d.decision IS NULL "
            "LIMIT 1",
            (workflow["id"], current),
        )
        return undecided is None

    def _validate_unresolved_retry_targets(
        self,
        workflow: dict[str, Any],
        node_id: str,
        requested: set[str],
    ) -> None:
        """After the first batch, refuse already-resolved targets on retry."""
        if node_id not in SUBGROUP_NODES or node_id == "03a":
            # 03a enforces stage-specific allowed subgroups separately.
            return
        if not self._node_has_completed_run(workflow["id"], node_id):
            return
        active = self._active_targets_for_node(workflow, node_id)
        unresolved = (
            active
            - self._node_target_resolution(workflow["id"], node_id)
            - self._autonomy_incomplete_targets_for_node(workflow["id"], node_id)
        )
        if not unresolved:
            raise AutonomyScopeError(
                f"All active {node_id} targets are already resolved or incomplete; "
                "do not schedule another run for this node"
            )
        if not requested or not requested.issubset(unresolved):
            raise AutonomyScopeError(
                f"{node_id} retry targets must be a non-empty subset of unresolved "
                f"targets {sorted(unresolved)}; already-resolved targets cannot be "
                "remeasured in the same node"
            )

    def _shared_parameter_retry_warnings(
        self,
        workflow: dict[str, Any],
        node_id: str,
        requested: set[str],
    ) -> list[str]:
        if node_id not in SUBGROUP_NODES:
            return []
        if not self._node_has_completed_run(workflow["id"], node_id):
            return []
        active = self._active_targets_for_node(workflow, node_id)
        unresolved = (
            active
            - self._node_target_resolution(workflow["id"], node_id)
            - self._autonomy_incomplete_targets_for_node(workflow["id"], node_id)
        )
        if len(requested) != 1 or len(unresolved) <= 1:
            return []
        return [
            f"{node_id} still has unresolved targets {sorted(unresolved)}; "
            "retry them together with the same parameters instead of isolating "
            f"{sorted(requested)} unless they require different sweep settings."
        ]

    def _validate_03a_run_request(
        self, workflow: dict[str, Any], parameters: dict[str, Any]
    ) -> None:
        workflow_id = workflow["id"]
        active = self._active_targets_for_node(workflow, "03a")
        requested = set(parameters["qubits"])
        rows = self.db.all(
            "SELECT status, analysis_status, analysis_json FROM runs "
            "WHERE workflow_id = ? AND node_id = '03a' AND status = 'completed'",
            (workflow_id,),
        )
        rules = self.policy.raw["analysis"]["03a"]
        if not rows:
            if requested != active:
                raise AutonomyScopeError(
                    "The first 03a coarse run must multiplex every active target"
                )
            if not math.isclose(
                float(parameters["frequency_span_in_mhz"]),
                float(rules["initial_coarse_span_mhz"]),
                abs_tol=1e-9,
            ):
                raise ServiceError("The first 03a coarse run must use an 800 MHz span")
            if float(parameters["operation_amplitude_factor"]) < float(
                rules["initial_coarse_operation_amplitude_factor"]
            ):
                raise ServiceError(
                    "The first 03a coarse run must use the configured stronger drive"
                )
            state = load_state(self.settings.active_state)
            nonzero = [
                name
                for name in sorted(active)
                if abs(float(state["qubits"][name]["xy"]["intermediate_frequency"]))
                > 1.0
            ]
            if nonzero:
                raise ServiceError(
                    "Apply the initial 03a zero-IF state proposal before the "
                    f"800 MHz run; nonzero IF targets: {nonzero}"
                )
            return

        if any(
            row.get("analysis_status") not in {"pass", "needs_review", "failed"}
            for row in rows
        ):
            raise ServiceError("Analyze the completed 03a run before requesting another")
        candidates = self._03a_candidate_targets(workflow_id)
        is_fine = (
            float(parameters["frequency_span_in_mhz"])
            <= float(rules["final_max_span_mhz"])
            and float(parameters["operation_amplitude_factor"])
            <= float(rules["final_max_operation_amplitude_factor"])
        )
        is_refinement = (
            float(parameters["frequency_span_in_mhz"])
            <= float(rules["refinement_max_span_mhz"])
            and float(parameters["operation_amplitude_factor"])
            <= float(rules["refinement_max_operation_amplitude_factor"])
        )
        allowed = candidates if (is_fine or is_refinement) else active - candidates
        if not requested or not requested.issubset(allowed):
            stage = "fine" if is_fine else "refinement" if is_refinement else "coarse"
            raise AutonomyScopeError(
                f"03a {stage} run targets are outside the allowed subgroup: "
                f"{sorted(allowed)}"
            )

    def _active_targets_for_node(
        self, workflow: dict[str, Any], node_id: str
    ) -> set[str]:
        targets = set(json_loads(workflow["targets_json"], []))
        sequence = list(self.settings.workflow_sequence)
        if node_id in sequence[3:]:
            _, absent_targets = self._02c_target_resolution(workflow["id"])
            targets -= absent_targets
        node_index = sequence.index(node_id)
        for upstream in sequence[:node_index]:
            if upstream not in SUBGROUP_NODES:
                continue
            advanced = self.db.one(
                "SELECT d.id FROM decisions d JOIN runs r ON r.id = d.run_id "
                "WHERE d.workflow_id = ? AND r.node_id = ? "
                "AND d.decision = 'advance' ORDER BY d.created_at DESC LIMIT 1",
                (workflow["id"], upstream),
            )
            if advanced is not None:
                targets &= self._node_target_resolution(workflow["id"], upstream)
        return targets

    def _complete_autonomy_scope_if_needed(
        self,
        workflow_id: str,
        next_node: str | None,
        actor: str,
    ) -> None:
        for lease in self.db.all(
            "SELECT id, allowed_nodes_json FROM autonomy_leases "
            "WHERE workflow_id = ? AND status = 'active'",
            (workflow_id,),
        ):
            allowed_nodes = json_loads(lease["allowed_nodes_json"], [])
            if next_node in allowed_nodes:
                continue
            reason_text = (
                "Authorized node range completed; a new human lease is required "
                "before the next node."
            )
            now = utc_now()
            self.db.execute(
                "UPDATE autonomy_leases SET status = 'completed', "
                "stopped_reason = ?, updated_at = ? WHERE id = ?",
                (reason_text, now, lease["id"]),
            )
            self.db.execute(
                "UPDATE measurement_sessions SET status = 'completed', "
                "updated_at = ? WHERE autonomy_lease_id = ?",
                (now, lease["id"]),
            )
            self.db.event(
                "autonomy_scope_completed",
                actor,
                {"lease_id": lease["id"], "next_node": next_node},
                workflow_id,
            )

    def _rewind_invalidated_subgroup_evidence(
        self,
        workflow: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        """Reopen 04/05 when a stricter policy invalidates prior advancement."""
        sequence = list(self.settings.workflow_sequence)
        current = workflow.get("current_node")
        if current not in sequence:
            return workflow
        current_index = sequence.index(current)
        for node_id in ("04", "05"):
            if node_id not in sequence or sequence.index(node_id) >= current_index:
                continue
            advanced = self.db.one(
                "SELECT d.id FROM decisions d JOIN runs r ON r.id = d.run_id "
                "WHERE d.workflow_id = ? AND r.node_id = ? "
                "AND d.decision = 'advance' ORDER BY d.created_at DESC LIMIT 1",
                (workflow["id"], node_id),
            )
            if advanced is None:
                continue
            eligible = self._active_targets_for_node(workflow, node_id)
            resolved = self._node_target_resolution(workflow["id"], node_id)
            missing = eligible - resolved
            if not missing:
                continue
            self.db.execute(
                "UPDATE workflows SET current_node = ?, updated_at = ? WHERE id = ?",
                (node_id, utc_now(), workflow["id"]),
            )
            self.db.event(
                "workflow_evidence_revalidation",
                actor,
                {
                    "reopened_node": node_id,
                    "missing_targets": sorted(missing),
                    "reason": (
                        "Current 04/05 robust-SNR policy invalidated prior "
                        "completion evidence before a new autonomy lease."
                    ),
                },
                workflow["id"],
            )
            return self._workflow(workflow["id"])
        return workflow

    def _validate_node_prerequisites(
        self,
        workflow: dict[str, Any],
        node_id: str,
        parameters: dict[str, Any],
    ) -> None:
        """Validate all baselines before one non-refitted statistics run."""
        prerequisites = {
            "05st": ("05", "t1_seconds"),
            "06st_t2star": ("06", "coherence_seconds"),
            "06st_t2e": ("06b", "coherence_seconds"),
        }
        if node_id not in prerequisites:
            return
        baseline_node, lifetime_key = prerequisites[node_id]
        requested = {str(name) for name in parameters.get("qubits", [])}
        reset_type = _reset_type(parameters)
        required_baselines = self._active_reset_verification_nodes()
        accepted: dict[str, dict[str, dict[str, Any]]] = {}
        missing_by_node: dict[str, list[str]] = {}
        for required_node in required_baselines:
            accepted[required_node] = {}
            for target in sorted(requested):
                fit = self._latest_accepted_target_fit(
                    workflow["id"], required_node, target, reset_type
                )
                if fit is None:
                    missing_by_node.setdefault(required_node, []).append(target)
                else:
                    accepted[required_node][target] = fit
        if missing_by_node:
            details = "; ".join(
                f"{name}: {targets}" for name, targets in missing_by_node.items()
            )
            qualifier = "repeated " if reset_type == "active" else ""
            raise ServiceError(
                f"{node_id} requires accepted {qualifier}05/06/06b evidence "
                f"using reset_type={reset_type} for every target; missing {details}"
            )
        maximum = float(parameters["max_wait_time_in_ns"])
        for target in sorted(requested):
            evidence = accepted[baseline_node][target]
            lifetime = evidence.get(lifetime_key) if evidence else None
            coverage = evidence.get("coverage_lifetimes") if evidence else None
            if not _finite_positive(lifetime):
                raise ServiceError(
                    f"{node_id} cannot resolve a finite {baseline_node} lifetime for {target}"
                )
            if not _finite_positive(coverage) or float(coverage) < 3.5:
                raise ServiceError(
                    f"{node_id} requires {baseline_node} to demonstrate decay to "
                    f"equilibrium (at least 3.5 lifetimes) for {target}"
                )
            lifetime_ns = float(lifetime) * 1e9
            ratio = maximum / lifetime_ns
            if not 3.5 <= ratio <= 5.5:
                raise ServiceError(
                    f"{node_id} max_wait_time_in_ns for {target} must be about four "
                    f"times the fitted lifetime; requested ratio is {ratio:.3g}"
                )

    def _active_reset_verification_nodes(self) -> tuple[str, ...]:
        configured = self.policy.raw.get("reset_policy", {}).get(
            "active_statistics_verification_nodes", ["05", "06", "06b"]
        )
        return tuple(str(node) for node in configured)

    def _is_active_reset_verification_node(
        self, workflow: dict[str, Any], node_id: str
    ) -> bool:
        if node_id not in self._active_reset_verification_nodes():
            return False
        sequence = list(self.settings.workflow_sequence)
        current = workflow.get("current_node")
        qualification_node = str(
            self.policy.raw.get("reset_policy", {}).get(
                "active_qualification_node", "07b"
            )
        )
        if current not in sequence or qualification_node not in sequence:
            return False
        return sequence.index(current) > sequence.index(qualification_node)

    def _validate_active_reset_prerequisite(
        self,
        workflow: dict[str, Any],
        node_id: str,
        parameters: dict[str, Any],
    ) -> None:
        if not _is_active_reset(parameters):
            return
        qualification_node = str(
            self.policy.raw.get("reset_policy", {}).get(
                "active_qualification_node", "07b"
            )
        )
        # 07b is the qualifying experiment, so it must be allowed to try active
        # reset before such evidence exists.
        if node_id == qualification_node:
            return
        requested = {str(name) for name in parameters.get("qubits", [])}
        qualified = self._active_reset_qualified_targets(workflow["id"])
        missing = requested - qualified
        if missing:
            floor = float(
                self.policy.raw.get("reset_policy", {}).get(
                    "active_min_readout_fidelity", 0.85
                )
            )
            raise ServiceError(
                "Active reset requires an accepted active-reset 07b result with "
                f"compact clouds and readout fidelity >= {floor:g}; missing "
                f"qualification for {sorted(missing)}"
            )

    def _active_reset_qualified_targets(self, workflow_id: str) -> set[str]:
        qualification_node = str(
            self.policy.raw.get("reset_policy", {}).get(
                "active_qualification_node", "07b"
            )
        )
        floor = float(
            self.policy.raw.get("reset_policy", {}).get(
                "active_min_readout_fidelity", 0.85
            )
        )
        qualified: set[str] = set()
        rows = self.db.all(
            "SELECT r.status, r.analysis_status, r.analysis_json, "
            "r.parameters_json, d.decision FROM runs r "
            "LEFT JOIN decisions d ON d.run_id = r.id "
            "WHERE r.workflow_id = ? AND r.node_id = ? ORDER BY r.rowid",
            (workflow_id, qualification_node),
        )
        for row in rows:
            if row.get("status") != "completed" or row.get("decision") not in {
                "advance",
                "repeat",
            }:
                continue
            parameters = json_loads(row.get("parameters_json"), {})
            if not _is_active_reset(parameters):
                continue
            analysis = json_loads(row.get("analysis_json"), {})
            results = analysis.get("fit_quality", {}).get("results", {})
            if not isinstance(results, dict):
                continue
            for target, fit in results.items():
                fidelity = fit.get("readout_fidelity") if isinstance(fit, dict) else None
                if (
                    isinstance(fit, dict)
                    and fit.get("fit_successful") is True
                    and fit.get("active_reset_qualified") is True
                    and fit.get("morphology_pass") is True
                    and _finite_positive(fidelity)
                    and floor <= float(fidelity) <= 1.0
                ):
                    qualified.add(str(target))
        return qualified

    def _latest_accepted_target_fit(
        self,
        workflow_id: str,
        node_id: str,
        target: str,
        reset_type: str,
    ) -> dict[str, Any] | None:
        rows = self.db.all(
            "SELECT r.status, r.analysis_status, r.analysis_json, "
            "r.parameters_json, d.decision FROM runs r "
            "LEFT JOIN decisions d ON d.run_id = r.id "
            "WHERE r.workflow_id = ? AND r.node_id = ? ORDER BY r.rowid DESC",
            (workflow_id, node_id),
        )
        for row in rows:
            # A multiplexed run may resolve this target while another target in
            # the same run causes the workflow-level decision to be repeat.
            if row.get("decision") not in {"advance", "repeat"}:
                continue
            run_parameters = json_loads(row.get("parameters_json"), {})
            if _reset_type(run_parameters) != reset_type:
                continue
            if target not in run_parameters.get("qubits", []):
                continue
            if target not in self._resolve_targets_from_rows(node_id, [row]):
                continue
            analysis = json_loads(row.get("analysis_json"), {})
            fit = analysis.get("fit_quality", {}).get("results", {}).get(target)
            if isinstance(fit, dict):
                return fit
        return None

    def _latest_target_fit(
        self, workflow_id: str, node_id: str, target: str
    ) -> dict[str, Any] | None:
        rows = self.db.all(
            "SELECT parameters_json, analysis_json, analysis_status FROM runs "
            "WHERE workflow_id = ? AND node_id = ? AND status = 'completed' "
            "ORDER BY started_at DESC",
            (workflow_id, node_id),
        )
        for row in rows:
            parameters = json_loads(row.get("parameters_json"), {})
            if target not in parameters.get("qubits", []):
                continue
            if row.get("analysis_status") not in {"pass", "needs_review"}:
                continue
            analysis = json_loads(row.get("analysis_json"), {})
            fit = analysis.get("fit_quality", {}).get("results", {}).get(target)
            if isinstance(fit, dict):
                return fit
        return None

    def _target_has_lifetime_coverage(
        self,
        workflow_id: str,
        node_id: str,
        target: str,
        minimum: float,
    ) -> bool:
        fit = self._latest_target_fit(workflow_id, node_id, target)
        coverage = fit.get("coverage_lifetimes") if fit else None
        return _finite_positive(coverage) and float(coverage) >= float(minimum)

    def _workflow(self, workflow_id: str) -> dict[str, Any]:
        workflow = self.db.one("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
        if workflow is None:
            raise ServiceError(f"Unknown workflow: {workflow_id}")
        return workflow

    def _reject_retained_hardware_lock(self) -> None:
        """Permit reconnecting to a live worker, but never start through quarantine."""
        if not self.settings.lock_path.exists():
            return
        active = self.db.one(
            "SELECT id, pid FROM runs WHERE status IN ('starting','running','stopping') "
            "ORDER BY started_at DESC LIMIT 1"
        )
        if active is not None:
            pid = active.get("pid")
            if isinstance(pid, int) and self.runner.process_is_alive(pid):
                return
        raise ServiceError(
            "A retained hardware lock places JY in recovery-only quarantine. "
            f"Use the local operator console ({self.operator_console_url}) to inspect "
            "and recover it before entering a new measurement mode. To close all "
            "remaining JY programs first, issue '恢復' or 'Recover'."
        )

    def _decode_workflow(self, row: dict[str, Any] | None) -> dict[str, Any]:
        if row is None:
            raise ServiceError("Workflow row is missing")
        result = dict(row)
        result["targets"] = json_loads(result.pop("targets_json"), [])
        result["initial_parameters"] = json_loads(
            result.pop("initial_parameters_json"), {}
        )
        return result

    def _decode_proposal(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json_loads(result.pop("payload_json"), {})
        delegated = bool(result.get("autonomy_lease_id")) and result["kind"] != "autonomy_lease"
        session = self.db.one(
            "SELECT id FROM measurement_sessions WHERE workflow_id = ? "
            "AND status IN ('active','paused','halted','stopping',"
            "'recovery_required') ORDER BY started_at DESC LIMIT 1",
            (result["workflow_id"],),
        )
        dashboard_session_id = str(session["id"]) if session is not None else None
        browser_url = (
            self.session_dashboard_url(dashboard_session_id)
            if session is not None
            else self.browser_approval_url(result["id"])
        )
        result["dashboard_session_id"] = dashboard_session_id
        result["browser_url"] = browser_url
        result["human_approval"] = {
            "required": result["status"] == "pending",
            "browser_url": browser_url,
            "confirmation": f"APPROVE {result['id']}",
            "command": f"jy-agent approve {result['id']}",
            "instruction": (
                "This action is delegated by the referenced human-approved autonomy "
                "lease; no additional proposal approval is required."
                if delegated
                else
                "A human must open browser_url, review the result plot(s), Decision, "
                "Reason, next action, and complete payload, then type the displayed "
                "confirmation. The terminal command is fallback only; no MCP "
                "approval tool exists."
            ),
        }
        if result["status"] == "pending" and not delegated:
            result["operator_handoff"] = approval_operator_handoff()
        return result

    def _decode_run(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["parameters"] = json_loads(result.pop("parameters_json"), {})
        result["analysis"] = json_loads(result.pop("analysis_json"), None)
        result["elapsed_seconds"] = _elapsed_seconds(result)
        return result


def _elapsed_seconds(row: dict[str, Any]) -> float | None:
    started_at = row.get("started_at")
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(str(started_at))
        finished_at = row.get("finished_at")
        finished = (
            datetime.fromisoformat(str(finished_at))
            if finished_at
            else datetime.now(timezone.utc)
        )
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        return max(0.0, (finished - started).total_seconds())
    except (TypeError, ValueError):
        return None


def _reset_type(parameters: Any) -> str:
    if not isinstance(parameters, dict):
        return "thermal"
    value = parameters.get(
        "reset_type",
        parameters.get("reset_type_thermal_or_active", "thermal"),
    )
    return str(value).strip().casefold()


def _is_active_reset(parameters: Any) -> bool:
    return _reset_type(parameters).startswith("active")


def _finite_positive(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
