from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from jy_agent.service import AgentService
from jy_agent.state import drive_lo_recenter_patch, load_state
from jy_agent.util import atomic_write_json
from test_core import make_settings, sample_state, start_test_workflow


def lo_state() -> dict:
    state = sample_state(0.2, 0.1)
    state["ports"] = {
        "mw_outputs": {
            "con1": {"7": {"3": {"upconverter_frequency": 3_900_000_000}}}
        }
    }
    state["qubits"]["q1"]["xy"]["intermediate_frequency"] = -263_125_000
    return state


def lo_wiring(*qubits: str) -> dict:
    return {
        "wiring": {
            "qubits": {
                name: {
                    "xy": {"opx_output": "#/ports/mw_outputs/con1/7/3"}
                }
                for name in qubits
            }
        }
    }


class DriveLoRecenterTests(unittest.TestCase):
    def test_patch_preserves_rf_on_100_mhz_lo_grid(self) -> None:
        patch, details = drive_lo_recenter_patch(
            lo_state(), lo_wiring("q1"), ["q1"]
        )
        self.assertEqual(
            patch,
            [
                {
                    "op": "replace",
                    "path": "/ports/mw_outputs/con1/7/3/upconverter_frequency",
                    "value": 3_600_000_000.0,
                },
                {
                    "op": "replace",
                    "path": "/qubits/q1/xy/intermediate_frequency",
                    "value": 36_875_000.0,
                },
            ],
        )
        self.assertEqual(details["q1"]["preserved_rf_hz"], 3_636_875_000.0)
        self.assertEqual(details["q1"]["lo_grid_hz"], 100_000_000.0)

    def test_initial_patch_preserves_rf_and_sets_if_to_zero(self) -> None:
        patch, details = drive_lo_recenter_patch(
            lo_state(),
            lo_wiring("q1"),
            ["q1"],
            force_zero_if=True,
        )
        self.assertEqual(patch[0]["value"], 3_636_875_000.0)
        self.assertEqual(patch[1]["value"], 0.0)
        self.assertEqual(details["q1"]["mode"], "preserve_rf_zero_if")

    def test_shifted_window_requires_100_mhz_grid(self) -> None:
        with self.assertRaisesRegex(Exception, "grid"):
            drive_lo_recenter_patch(
                lo_state(),
                lo_wiring("q1"),
                ["q1"],
                target_lo_hz={"q1": 3_650_000_000.0},
            )

    def test_candidate_center_uses_grid_lo_and_residual_if(self) -> None:
        patch, details = drive_lo_recenter_patch(
            lo_state(),
            lo_wiring("q1"),
            ["q1"],
            target_rf_hz={"q1": 3_636_875_000.0},
        )
        self.assertEqual(patch[0]["value"], 3_600_000_000.0)
        self.assertEqual(patch[1]["value"], 36_875_000.0)
        self.assertEqual(
            details["q1"]["mode"], "candidate_center_preserve_rf"
        )
        self.assertEqual(details["q1"]["new_rf_hz"], 3_636_875_000.0)

    def test_patch_rejects_shared_xy_output(self) -> None:
        state = lo_state()
        state["qubits"]["q2"] = deepcopy(state["qubits"]["q1"])
        with self.assertRaisesRegex(Exception, "shared"):
            drive_lo_recenter_patch(
                state, lo_wiring("q1", "q2"), ["q1"]
            )

    def test_service_creates_pending_proposal_without_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), lo_state())
            atomic_write_json(settings.wiring_path, lo_wiring("q1"))
            service = AgentService(settings)
            workflow = start_test_workflow(service)
            service.db.execute(
                "UPDATE workflows SET current_node = '03a' WHERE id = ?",
                (workflow["id"],),
            )
            proposal = service.request_drive_lo_recenter(
                workflow["id"], ["q1"], "unittest"
            )
            self.assertEqual(proposal["kind"], "state_commit")
            self.assertEqual(proposal["status"], "pending")
            self.assertEqual(
                proposal["drive_lo_recenter"]["q1"]["new_lo_hz"],
                3_600_000_000.0,
            )
            self.assertEqual(
                proposal["drive_lo_recenter"]["q1"]["new_if_hz"],
                36_875_000.0,
            )
            self.assertEqual(
                load_state(settings.active_state)["qubits"]["q1"]["xy"]
                ["intermediate_frequency"],
                -263_125_000,
            )

    def test_service_centers_credible_unresolved_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), lo_state())
            atomic_write_json(settings.wiring_path, lo_wiring("q1"))
            service = AgentService(settings)
            workflow = start_test_workflow(service)
            service.db.execute(
                "UPDATE workflows SET current_node = '03a' WHERE id = ?",
                (workflow["id"],),
            )
            source_proposal = service.request_drive_lo_recenter(
                workflow["id"], ["q1"], "unittest"
            )
            analysis = {
                "03a_stage": "coarse_candidate",
                "fit_quality": {
                    "results": {
                        "q1": {
                            "fit_successful": True,
                            "drive_freq": 3_636_875_000.0,
                        }
                    }
                },
                "dataset_metrics": {
                    "qubits": {
                        "q1": {
                            "edge_fraction": 0.4,
                            "robust_snr": 12.0,
                            "feature_fwhm_hz": 12_000_000.0,
                        }
                    }
                },
            }
            service.db.execute(
                """
                INSERT INTO runs(
                    id, workflow_id, proposal_id, node_id, parameters_json,
                    status, analysis_status, analysis_json
                ) VALUES (?, ?, ?, '03a', ?, 'completed', 'pass', ?)
                """,
                (
                    "candidate-run",
                    workflow["id"],
                    source_proposal["id"],
                    json.dumps(
                        {
                            "qubits": ["q1"],
                            "frequency_span_in_mhz": 800.0,
                            "operation_amplitude_factor": 0.10,
                        }
                    ),
                    json.dumps(analysis),
                ),
            )

            proposal = service.request_03a_candidate_center(
                workflow["id"], ["q1"], "unittest"
            )
            self.assertEqual(proposal["kind"], "state_commit")
            details = proposal["03a_candidate_center"]["q1"]
            self.assertEqual(details["new_lo_hz"], 3_600_000_000.0)
            self.assertEqual(details["new_if_hz"], 36_875_000.0)
            self.assertEqual(
                details["candidate_evidence"]["run_id"], "candidate-run"
            )


if __name__ == "__main__":
    unittest.main()
