from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from starlette.requests import Request

from jy_agent.approval_web import handle_session_dashboard
from jy_agent.dashboard_access import DashboardAccessManager
from jy_agent.db import Database
from jy_agent.operator_web import handle_operator_console
from jy_agent.recovery import HardwareLockRecovery
from jy_agent.service import AgentService
from jy_agent.util import atomic_write_json, utc_now

from test_autonomy import AUTO_PHRASE
from test_core import ENTRY_PHRASE, make_settings, sample_state
from test_recovery import (
    RUN_ID,
    make_settings as make_recovery_settings,
    seed_retained_lock,
)


def _request(
    path: str,
    path_params: dict[str, object] | None = None,
    *,
    method: str = "GET",
    body: bytes = b"",
    port: int = 8766,
    client_host: str = "127.0.0.1",
    request_host: str = "127.0.0.1",
) -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"host", f"{request_host}:{port}".encode("ascii"))]
    if method == "POST":
        headers.extend(
            (
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"origin", f"http://127.0.0.1:{port}".encode("ascii")),
            )
        )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "client": (client_host, 50123),
            "server": ("127.0.0.1", port),
            "path_params": path_params or {},
        },
        receive,
    )


class FullShutdownWebTests(unittest.TestCase):
    def test_home_explains_ai_disconnect_and_recover_command(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(
                make_settings(Path(folder), sample_state(0.2, 0.1))
            )
            entered = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            session_id = entered["dashboard"]["id"]
            page = asyncio.run(
                handle_session_dashboard(
                    _request(
                        f"/session/{session_id}/home", {"session_id": session_id}
                    ),
                    service,
                    view="home",
                )
            ).body.decode("utf-8")
            self.assertIn("AI／網路中斷時怎麼辦", page)
            self.assertIn("結束量測", page)
            self.assertIn("Recover", page)

    def test_instrument_outage_is_rendered_as_paused_not_crashed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(
                make_settings(Path(folder), sample_state(0.2, 0.1))
            )
            entered = service.enter_autonomy_mode(
                "unittest-agent", AUTO_PHRASE, ["q1"]
            )
            proposal_id = entered["approval"]["id"]
            lease_id = entered["approval"]["autonomy_lease_id"]
            session_id = entered["dashboard"]["id"]
            service._approve_pending_proposal(
                proposal_id, "unit-test-human", "unit_test"
            )
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status, analysis_status, analysis_json, "
                "termination_cause, autonomy_lease_id) VALUES "
                "('web-instrument-outage', ?, ?, '02x', '{}', 'failed', "
                "'failed', ?, 'instrument_unreachable', ?)",
                (
                    entered["workflow"]["id"],
                    proposal_id,
                    json.dumps(
                        {
                            "analysis_status": "failed",
                            "failure_category": "instrument_unreachable",
                            "hardware_lock_recovery_required": False,
                            "operator_message": {
                                "zh-Hant": "儀器連線失敗，本次實驗與後續排程已暫停。",
                                "en": "Instrument connectivity failed.",
                            },
                            "plots": [],
                        },
                        ensure_ascii=False,
                    ),
                    lease_id,
                ),
            )

            page = asyncio.run(
                handle_session_dashboard(
                    _request(
                        f"/session/{session_id}/results",
                        {"session_id": session_id},
                    ),
                    service,
                    view="results",
                )
            ).body.decode("utf-8")

            self.assertIn("儀器錯誤：量測已暫停", page)
            self.assertIn("儀器連線失敗", page)
            self.assertEqual(
                service.autonomy_status(lease_id=lease_id)["status"], "paused"
            )

    def test_home_resume_after_instrument_outage_keeps_active_controls(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(
                make_settings(Path(folder), sample_state(0.2, 0.1))
            )
            entered = service.enter_autonomy_mode(
                "unittest-agent", AUTO_PHRASE, ["q1"]
            )
            proposal_id = entered["approval"]["id"]
            lease_id = entered["approval"]["autonomy_lease_id"]
            session_id = entered["dashboard"]["id"]
            service._approve_pending_proposal(
                proposal_id, "unit-test-human", "unit_test"
            )
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status, analysis_status, analysis_json, "
                "termination_cause, autonomy_lease_id) VALUES "
                "('web-instrument-outage', ?, ?, '02x', '{}', 'failed', "
                "'failed', ?, 'instrument_unreachable', ?)",
                (
                    entered["workflow"]["id"],
                    proposal_id,
                    json.dumps(
                        {
                            "analysis_status": "failed",
                            "failure_category": "instrument_unreachable",
                            "hardware_lock_recovery_required": False,
                            "operator_message": {
                                "zh-Hant": "儀器連線失敗，本次實驗與後續排程已暫停。",
                                "en": "Instrument connectivity failed.",
                            },
                            "plots": [],
                        },
                        ensure_ascii=False,
                    ),
                    lease_id,
                ),
            )
            asyncio.run(
                handle_session_dashboard(
                    _request(
                        f"/session/{session_id}/results",
                        {"session_id": session_id},
                    ),
                    service,
                    view="results",
                )
            )
            form = urlencode(
                {
                    "operation": "control",
                    "action": "resume",
                    "csrf_token": service.autonomy_csrf_token(lease_id),
                }
            ).encode("ascii")
            page = asyncio.run(
                handle_session_dashboard(
                    _request(
                        f"/session/{session_id}/home",
                        {"session_id": session_id},
                        method="POST",
                        body=form,
                    ),
                    service,
                    view="home",
                )
            ).body.decode("utf-8")

            self.assertEqual(
                service.autonomy_status(lease_id=lease_id)["status"], "active"
            )
            self.assertIn("控制已套用：進行中。", page)
            self.assertIn("目前實驗完成後暫停排程", page)
            self.assertNotIn("繼續排程", page)
            self.assertNotIn("儀器錯誤：量測已暫停", page)

    def test_full_shutdown_requires_exact_second_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(
                make_settings(Path(folder), sample_state(0.2, 0.1))
            )
            entered = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            session_id = entered["dashboard"]["id"]
            form = urlencode(
                {
                    "operation": "shutdown",
                    "csrf_token": service.dashboard_shutdown_csrf_token(session_id),
                    "confirmation": "SHUTDOWN wrong-session",
                }
            ).encode("ascii")
            response = asyncio.run(
                handle_session_dashboard(
                    _request(
                        f"/session/{session_id}/home",
                        {"session_id": session_id},
                        method="POST",
                        body=form,
                    ),
                    service,
                    view="home",
                )
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(entered["workflow"]["status"], "active")
            self.assertFalse((service.settings.runtime / "full-shutdown-request.json").exists())

    def test_halted_automation_page_keeps_full_shutdown_control(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(
                make_settings(Path(folder), sample_state(0.2, 0.1))
            )
            entered = service.enter_autonomy_mode(
                "unittest-agent", AUTO_PHRASE, ["q1"]
            )
            proposal_id = entered["approval"]["id"]
            lease_id = entered["approval"]["autonomy_lease_id"]
            session_id = entered["dashboard"]["id"]
            service._approve_pending_proposal(
                proposal_id, "unit-test-human", "unit_test"
            )
            service._halt_autonomy(
                lease_id,
                "Synthetic policy halt for Dashboard coverage.",
                "unittest",
                stop_active=False,
            )

            page = asyncio.run(
                handle_session_dashboard(
                    _request(
                        f"/session/{session_id}/home", {"session_id": session_id}
                    ),
                    service,
                    view="home",
                )
            )
            body = page.body.decode("utf-8")
            self.assertIn("已中止", body)
            self.assertIn('name="operation" value="shutdown"', body)
            self.assertIn("結束量測並關閉所有 JY 服務", body)

            form = urlencode(
                {
                    "operation": "shutdown",
                    "csrf_token": service.dashboard_shutdown_csrf_token(session_id),
                    "confirmation": service.dashboard_shutdown_confirmation(session_id),
                }
            ).encode("ascii")
            response = asyncio.run(
                handle_session_dashboard(
                    _request(
                        f"/session/{session_id}/home",
                        {"session_id": session_id},
                        method="POST",
                        body=form,
                    ),
                    service,
                    view="home",
                )
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                service.db.one(
                    "SELECT status FROM workflows WHERE id = ?",
                    (entered["workflow"]["id"],),
                )["status"],
                "stopped",
            )
            self.assertEqual(service.dashboard_status(session_id)["status"], "stopped")
            self.assertEqual(
                service.dashboard_status(session_id)["shutdown"]["status"], "ready"
            )

    def test_retained_lock_queues_shutdown_without_deleting_lock(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            service = AgentService(settings)
            entered = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            session_id = entered["dashboard"]["id"]
            atomic_write_json(settings.lock_path, {"run_id": "retained-test-lock"})

            result = service.request_full_shutdown(
                session_id, "browser operator"
            )
            self.assertEqual(result["status"], "quarantine_ready")
            self.assertTrue(settings.lock_path.exists())
            self.assertEqual(service.dashboard_status(session_id)["status"], "recovery_required")
            self.assertEqual(
                service.db.one(
                    "SELECT status FROM workflows WHERE id = ?",
                    (entered["workflow"]["id"],),
                )["status"],
                "recovery_required",
            )

            page = asyncio.run(
                handle_session_dashboard(
                    _request(
                        f"/session/{session_id}/home", {"session_id": session_id}
                    ),
                    service,
                    view="home",
                )
            ).body.decode("utf-8")
            self.assertIn("recovery quarantine", page)
            self.assertIn("http://127.0.0.1:8765/operator", page)

    def test_active_run_is_stopped_and_coordinator_waits_for_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            service = AgentService(settings)
            entered = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            workflow_id = entered["workflow"]["id"]
            session_id = entered["dashboard"]["id"]
            proposal = service.request_run(
                workflow_id,
                "02x",
                {"qubits": ["q1"]},
                "Synthetic active run for shutdown coverage.",
                "unittest-agent",
            )
            now = utc_now()
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status, started_at, analysis_status) "
                "VALUES ('active-shutdown-run', ?, ?, '02x', '{}', 'running', ?, "
                "'not_started')",
                (workflow_id, proposal["id"], now),
            )

            def mark_stopping(
                run_id: str, actor: str, **kwargs: str
            ) -> dict[str, str]:
                service.db.execute(
                    "UPDATE runs SET status = 'stopping', stop_intent = ? WHERE id = ?",
                    (kwargs.get("intent"), run_id),
                )
                return {"run_id": run_id, "status": "stopping", "actor": actor}

            with patch.object(
                service.runner, "stop", side_effect=mark_stopping
            ) as stop:
                waiting = service.request_full_shutdown(
                    session_id, "browser operator"
                )
            self.assertEqual(waiting["status"], "waiting_for_run")
            stop.assert_called_once_with(
                "active-shutdown-run",
                "browser operator",
                intent="full_shutdown",
                reason="Full measurement shutdown requested from Dashboard.",
            )
            self.assertEqual(service.dashboard_status(session_id)["status"], "stopping")

            service.db.execute(
                "UPDATE runs SET status = 'force_stopped', finished_at = ? WHERE id = ?",
                (utc_now(), "active-shutdown-run"),
            )
            ready = service.reconcile_full_shutdown_request()
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(service.dashboard_status(session_id)["status"], "stopped")

    def test_ready_request_dispatches_verified_stop_helper_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            service = AgentService(settings)
            entered = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            session_id = entered["dashboard"]["id"]
            service.request_full_shutdown(session_id, "browser operator")
            request = service._load_shutdown_request(session_id)
            request["dispatch_after"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(timespec="seconds")
            service._save_shutdown_request(request)

            with patch("jy_agent.service.subprocess.Popen") as popen:
                dispatched = service.reconcile_full_shutdown_request()
                again = service.reconcile_full_shutdown_request()

            self.assertEqual(dispatched["status"], "dispatching")
            self.assertEqual(again["status"], "dispatching")
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertIn(str(settings.agent_root / "stop_server.ps1"), command)
            self.assertEqual(command[-2:], ["-SessionId", session_id])

    def test_dispatch_failure_is_visible_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            service = AgentService(settings)
            entered = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            session_id = entered["dashboard"]["id"]
            service.request_full_shutdown(session_id, "browser operator")
            request = service._load_shutdown_request(session_id)
            request["dispatch_after"] = "2000-01-01T00:00:00+00:00"
            service._save_shutdown_request(request)

            with patch(
                "jy_agent.service.subprocess.Popen",
                side_effect=OSError("synthetic start failure"),
            ):
                failed = service.reconcile_full_shutdown_request()
            self.assertEqual(failed["status"], "failed")

            page = asyncio.run(
                handle_session_dashboard(
                    _request(
                        f"/session/{session_id}/home", {"session_id": session_id}
                    ),
                    service,
                    view="home",
                )
            ).body.decode("utf-8")
            self.assertIn("失敗", page)
            self.assertIn('name="operation" value="shutdown"', page)

    def test_async_stop_helper_failure_returns_dispatch_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            service = AgentService(settings)
            entered = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            session_id = entered["dashboard"]["id"]
            service.request_full_shutdown(session_id, "browser operator")
            request = service._load_shutdown_request(session_id)
            request["dispatch_after"] = "2000-01-01T00:00:00+00:00"
            service._save_shutdown_request(request)

            with patch("jy_agent.service.subprocess.Popen"):
                dispatched = service.reconcile_full_shutdown_request(session_id)
            self.assertEqual(dispatched["status"], "dispatching")
            failure_path = (
                settings.runtime / "shutdown-stop-failures" / f"{session_id}.json"
            )
            atomic_write_json(
                failure_path,
                {
                    "session_id": session_id,
                    "failed_at": utc_now(),
                    "error": "synthetic asynchronous stop failure",
                },
            )

            failed = service.reconcile_full_shutdown_request(session_id)

            self.assertEqual(failed["status"], "failed")
            self.assertIn("synthetic asynchronous", failed["error"])

    def test_shutdown_requests_are_isolated_by_dashboard_session(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            service = AgentService(settings)
            first = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            first_session = first["dashboard"]["id"]
            service.request_full_shutdown(first_session, "browser operator")

            second = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            second_session = second["dashboard"]["id"]
            service.request_full_shutdown(second_session, "browser operator")

            self.assertNotEqual(first_session, second_session)
            self.assertEqual(
                service._shutdown_request_for_session(first_session)["session_id"],
                first_session,
            )
            self.assertEqual(
                service._shutdown_request_for_session(second_session)["session_id"],
                second_session,
            )
            self.assertTrue(
                (settings.runtime / "shutdown_requests" / f"{first_session}.json").is_file()
            )
            self.assertTrue(
                (settings.runtime / "shutdown_requests" / f"{second_session}.json").is_file()
            )

    def test_recovery_only_instance_finishes_prior_quarantine_request(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"JY_SERVICE_INSTANCE_NONCE": "old-instance"}
        ):
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            service = AgentService(settings)
            entered = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            session_id = entered["dashboard"]["id"]
            atomic_write_json(settings.lock_path, {"run_id": "retained-test-lock"})
            queued = service.request_full_shutdown(session_id, "browser operator")
            queued["dispatch_after"] = "2000-01-01T00:00:00+00:00"
            service._save_shutdown_request(queued)
            with patch("jy_agent.service.subprocess.Popen"):
                dispatched = service.reconcile_full_shutdown_request(session_id)
            self.assertEqual(dispatched["status"], "dispatching")

            # Simulate the formal recovery object having archived the lock before
            # the new recovery-only service reconciles the prior session.
            settings.lock_path.unlink()
            with patch.dict(
                os.environ, {"JY_SERVICE_INSTANCE_NONCE": "new-instance"}
            ):
                recovered_service = AgentService(settings)
                ready = recovered_service.reconcile_full_shutdown_request(session_id)

            self.assertEqual(ready["status"], "ready")
            self.assertEqual(
                recovered_service.db.one(
                    "SELECT status FROM workflows WHERE id = ?",
                    (entered["workflow"]["id"],),
                )["status"],
                "stopped",
            )


class LocalOperatorWebTests(unittest.TestCase):
    def test_operator_console_is_loopback_only_without_pairing_ui(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(
                make_settings(Path(folder), sample_state(0.2, 0.1)),
                approval_transport="public",
                approval_public_base_url="https://jy-public.example.com",
                approval_access_token="a" * 64,
            )
            service = AgentService(settings)
            access = DashboardAccessManager(service.db, instance_nonce="local-development")

            remote = asyncio.run(
                handle_operator_console(
                    _request("/operator", port=8765, client_host="192.0.2.44"),
                    service,
                    access,
                )
            )
            self.assertEqual(remote.status_code, 403)
            self.assertNotIn(
                "Paired devices", remote.body.decode("utf-8")
            )
            rebound_host = asyncio.run(
                handle_operator_console(
                    _request(
                        "/operator",
                        port=8765,
                        client_host="127.0.0.1",
                        request_host="attacker.example",
                    ),
                    service,
                    access,
                )
            )
            self.assertEqual(rebound_host.status_code, 403)

            response = asyncio.run(
                handle_operator_console(
                    _request("/operator", port=8765),
                    service,
                    access,
                )
            )
            body = response.body.decode("utf-8")
            self.assertEqual(response.status_code, 200)
            self.assertIn("dashboard-password.txt", body)
            self.assertIn("There is no device-pairing step", body)
            self.assertNotIn("Pair another phone or browser", body)
            self.assertNotIn("/device-pair", body)
            self.assertNotIn(settings.approval_access_token, body)

    def test_operator_console_performs_formal_lock_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_recovery_settings(Path(folder))
            database = Database(settings.database_path)
            seed_retained_lock(settings, database)
            service = AgentService(settings)
            access = DashboardAccessManager(service.db, instance_nonce="local-development")
            session_id = "operator-recovery-session"
            now = utc_now()
            service.db.execute(
                "INSERT INTO measurement_sessions(id, workflow_id, mode, status, "
                "started_at, updated_at, source_client) "
                "VALUES (?, ?, 'autonomous', 'halted', ?, ?, 'unittest')",
                (session_id, "b" * 32, now, now),
            )
            queued = service.request_full_shutdown(session_id, "browser operator")
            self.assertEqual(queued["status"], "quarantine_ready")

            with patch.object(
                HardwareLockRecovery, "_active_worker_pids", return_value=(True, [])
            ):
                page = asyncio.run(
                    handle_operator_console(
                        _request("/operator", port=8765), service, access
                    )
                ).body.decode("utf-8")
            self.assertIn(f"RECOVER HARDWARE LOCK {RUN_ID}", page)

            form = urlencode(
                {
                    "operation": "recover_lock",
                    "csrf_token": service.operator_csrf_token(),
                    "run_id": RUN_ID,
                    "hardware_attestation": "confirmed",
                    "confirmation": f"RECOVER HARDWARE LOCK {RUN_ID}",
                }
            ).encode("ascii")
            with patch.object(
                HardwareLockRecovery, "_active_worker_pids", return_value=(True, [])
            ):
                response = asyncio.run(
                    handle_operator_console(
                        _request(
                            "/operator", method="POST", body=form, port=8765
                        ),
                        service,
                        access,
                    )
                )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(settings.lock_path.exists())
            self.assertTrue(
                (settings.runtime / "recovery" / f"{RUN_ID}-hardware-lock-recovery.json").is_file()
            )
            self.assertEqual(
                service.reconcile_full_shutdown_request()["status"], "ready"
            )
            self.assertEqual(service.dashboard_status(session_id)["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
