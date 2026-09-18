from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jy_agent.runner import _verify_worker_exit_receipt
from jy_agent.service import AgentService
from jy_agent.worker import _write_signed_exit_receipt
from jy_agent.util import sha256_file, utc_now
from jy_agent.worker import _persist_recovery_checkpoint

from test_core import ENTRY_PHRASE, make_settings, sample_state


class RunnerShutdownProtocolTests(unittest.TestCase):
    def test_worker_exit_receipt_is_bound_to_process_token(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "exit.json"
            payload = {
                "run_id": "signed-run",
                "termination_cause": "instrument_unreachable",
                "cleanup_verified": True,
                "lock_release_authorized": True,
                "lock_released": True,
            }
            _write_signed_exit_receipt(path, payload, "worker-secret")
            receipt = json.loads(path.read_text(encoding="utf-8"))

            self.assertTrue(
                _verify_worker_exit_receipt(receipt, "worker-secret")
            )
            self.assertFalse(
                _verify_worker_exit_receipt(receipt, "different-secret")
            )
            receipt["lock_released"] = False
            self.assertFalse(
                _verify_worker_exit_receipt(receipt, "worker-secret")
            )

    def test_worker_persists_recovery_checkpoint_before_hardware(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(
                make_settings(Path(folder), sample_state(0.2, 0.1))
            )
            entered = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            workflow_id = entered["workflow"]["id"]
            proposal = service.request_run(
                workflow_id,
                "02x",
                {"qubits": ["q1"]},
                "Synthetic checkpoint coverage.",
                "unittest-agent",
            )
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status, started_at) "
                "VALUES ('checkpoint-run', ?, ?, '02x', '{}', 'starting', ?)",
                (workflow_id, proposal["id"], utc_now()),
            )
            recovery_path = service.settings.runtime / "recovery" / "checkpoint-state.json"
            state_hash = sha256_file(service.settings.active_state)

            _persist_recovery_checkpoint(
                service.db,
                run_id="checkpoint-run",
                workflow_id=workflow_id,
                state_hash=state_hash,
                recovery_path=recovery_path,
            )

            run = service.db.one(
                "SELECT active_state_hash_before FROM runs WHERE id = ?",
                ("checkpoint-run",),
            )
            self.assertEqual(run["active_state_hash_before"], state_hash)
            event = service.db.one(
                "SELECT payload_json FROM events WHERE event_type = "
                "'run_recovery_checkpointed' ORDER BY id DESC LIMIT 1"
            )
            self.assertEqual(json.loads(event["payload_json"])["state_sha256"], state_hash)

    def test_stop_writes_authenticated_request_without_console_signal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(
                make_settings(Path(folder), sample_state(0.2, 0.1))
            )
            entered = service.enter_measurement_mode(
                "unittest-agent", ENTRY_PHRASE, ["q1"]
            )
            workflow_id = entered["workflow"]["id"]
            proposal = service.request_run(
                workflow_id,
                "02x",
                {"qubits": ["q1"]},
                "Synthetic cooperative-stop coverage.",
                "unittest-agent",
            )
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status, started_at, pid, process_token) "
                "VALUES ('cooperative-stop-run', ?, ?, '02x', '{}', 'running', "
                "?, 4242, 'secret-worker-token')",
                (workflow_id, proposal["id"], utc_now()),
            )

            with patch.object(
                service.runner, "_verify_worker_process"
            ), patch("jy_agent.runner.os.kill") as os_kill:
                result = service.runner.stop(
                    "cooperative-stop-run",
                    "browser operator",
                    intent="full_shutdown",
                    reason="test shutdown",
                )

            self.assertEqual(result["delivery"], "cooperative_stop_request")
            os_kill.assert_not_called()
            request_path = (
                service.settings.runtime
                / "requests"
                / "cooperative-stop-run.stop.json"
            )
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "cooperative-stop-run")
            self.assertEqual(payload["process_token"], "secret-worker-token")
            self.assertEqual(payload["intent"], "full_shutdown")
            run = service.db.one(
                "SELECT status, stop_intent, stop_requested_at FROM runs WHERE id = ?",
                ("cooperative-stop-run",),
            )
            self.assertEqual(run["status"], "stopping")
            self.assertEqual(run["stop_intent"], "full_shutdown")
            self.assertTrue(run["stop_requested_at"])

    def test_exit_evidence_never_returns_process_token(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(
                make_settings(Path(folder), sample_state(0.2, 0.1))
            )
            request_dir = service.settings.runtime / "requests"
            request_dir.mkdir(parents=True, exist_ok=True)
            (request_dir / "evidence-run.stop.json").write_text(
                json.dumps(
                    {
                        "run_id": "evidence-run",
                        "process_token": "must-not-leak",
                        "intent": "full_shutdown",
                    }
                ),
                encoding="utf-8",
            )
            (request_dir / "evidence-run.exit.json").write_text(
                json.dumps(
                    {
                        "run_id": "evidence-run",
                        "process_token": "must-not-leak",
                        "termination_cause": "cooperative_full_shutdown",
                    }
                ),
                encoding="utf-8",
            )

            evidence = service.runner.exit_evidence("evidence-run")

            self.assertNotIn("process_token", evidence["stop_request"])
            self.assertNotIn("process_token", evidence["exit_receipt"])
            self.assertEqual(
                evidence["exit_receipt"]["termination_cause"],
                "cooperative_full_shutdown",
            )


if __name__ == "__main__":
    unittest.main()
