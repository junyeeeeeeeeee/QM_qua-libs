from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jy_agent.service import AgentService
from jy_agent.util import atomic_write_json
from jy_agent.workflow_subgroups import (
    resolve_03a_candidate_targets,
    resolve_03a_shift_ready_targets,
    resolve_node_targets,
)
from tests.test_core import (
    ENTRY_PHRASE,
    make_settings,
    sample_multiplex_state_and_wiring,
    start_test_workflow,
)


def analysis_for(node_id: str, names: list[str]) -> dict:
    if node_id == "03a":
        results = {
            name: {"fit_successful": True, "drive_freq": 5.0e9}
            for name in names
        }
    elif node_id == "05":
        results = {
            name: {
                "t1_seconds": 35e-6,
                "relative_uncertainty": 0.1,
                "r_squared": 0.95,
                "coverage_lifetimes": 4.0,
                "samples_per_lifetime": 10.0,
                "fit_at_search_boundary": False,
            }
            for name in names
        }
    else:
        results = {name: {"Pi_amplitude": 0.21} for name in names}
    return {
        "03a_stage": "fine" if node_id == "03a" else None,
        "fit_quality": {"results": results},
        "dataset_metrics": {
            "qubits": {
                name: {
                    "edge_fraction": 0.4, "robust_snr": 30.0,
                    "feature_fwhm_hz": 7_500_000.0,
                }
                for name in names
            }
        },
    }


class TestWorkflowSubgroups(unittest.TestCase):
    def test_mixed_03a_run_resolves_only_usable_qubits(self) -> None:
        analysis = analysis_for("03a", ["q1", "q2"])
        analysis["fit_quality"]["results"]["q2"]["fit_successful"] = False
        resolved = resolve_node_targets(
            "03a",
            [
                {
                    "status": "completed",
                    "analysis_status": "needs_review",
                    "analysis_json": json.dumps(analysis),
                }
            ],
        )
        self.assertEqual(resolved, {"q1"})

    def test_03a_coarse_peak_is_candidate_but_not_final(self) -> None:
        analysis = analysis_for("03a", ["q1"])
        analysis["03a_stage"] = "coarse_candidate"
        rows = [
            {
                "status": "completed",
                "analysis_status": "pass",
                "analysis_json": json.dumps(analysis),
            }
        ]
        self.assertEqual(resolve_03a_candidate_targets(rows), {"q1"})
        self.assertEqual(resolve_node_targets("03a", rows), set())

    def test_03a_broad_fine_peak_is_not_final(self) -> None:
        analysis = analysis_for("03a", ["q1"])
        analysis["dataset_metrics"]["qubits"]["q1"][
            "feature_fwhm_hz"
        ] = 22_000_000.0
        rows = [{
            "status": "completed",
            "analysis_status": "needs_review",
            "analysis_json": json.dumps(analysis),
        }]
        self.assertEqual(resolve_03a_candidate_targets(rows), {"q1"})
        self.assertEqual(resolve_node_targets("03a", rows), set())

    def test_03a_fine_peak_below_half_mhz_is_not_final(self) -> None:
        analysis = analysis_for("03a", ["q1"])
        analysis["dataset_metrics"]["qubits"]["q1"][
            "feature_fwhm_hz"
        ] = 400_000.0
        rows = [{
            "status": "completed",
            "analysis_status": "needs_review",
            "analysis_json": json.dumps(analysis),
        }]
        self.assertEqual(resolve_03a_candidate_targets(rows), {"q1"})
        self.assertEqual(resolve_node_targets("03a", rows), set())

    def test_03a_fine_peak_above_half_mhz_can_be_final(self) -> None:
        analysis = analysis_for("03a", ["q1"])
        analysis["dataset_metrics"]["qubits"]["q1"][
            "feature_fwhm_hz"
        ] = 1_250_000.0
        rows = [{
            "status": "completed",
            "analysis_status": "pass",
            "analysis_json": json.dumps(analysis),
        }]
        self.assertEqual(resolve_node_targets("03a", rows), {"q1"})

    def test_03a_candidate_requires_snr_of_ten(self) -> None:
        analysis = analysis_for("03a", ["q1", "q5"])
        analysis["03a_stage"] = "coarse_candidate"
        analysis["dataset_metrics"]["qubits"]["q1"]["robust_snr"] = 7.6
        analysis["dataset_metrics"]["qubits"]["q5"]["robust_snr"] = 10.1
        rows = [
            {
                "status": "completed",
                "analysis_status": "needs_review",
                "analysis_json": json.dumps(analysis),
            }
        ]
        self.assertEqual(resolve_03a_candidate_targets(rows), {"q5"})

    def test_03a_noisy_interior_trace_must_reach_2000_before_shift(self) -> None:
        analysis = analysis_for("03a", ["q1", "q2"])
        analysis["03a_stage"] = "coarse_candidate"
        analysis["dataset_metrics"]["qubits"]["q1"]["robust_snr"] = 7.5
        analysis["dataset_metrics"]["qubits"]["q2"].update(
            {"edge_fraction": 0.01, "robust_snr": 5.4}
        )
        analysis["fit_quality"]["results"]["q2"]["fit_successful"] = False
        row = {
            "status": "completed",
            "analysis_status": "needs_review",
            "analysis_json": json.dumps(analysis),
            "parameters_json": json.dumps(
                {"qubits": ["q1", "q2"], "num_averages": 100}
            ),
        }
        self.assertEqual(resolve_03a_shift_ready_targets([row]), {"q2"})
        row["parameters_json"] = json.dumps(
            {"qubits": ["q1", "q2"], "num_averages": 2000}
        )
        self.assertEqual(
            resolve_03a_shift_ready_targets([row]),
            {"q1", "q2"},
        )

    def test_manual_review_rejects_false_candidate_and_allows_shift(self) -> None:
        candidate = analysis_for("03a", ["q1"])
        candidate["03a_stage"] = "coarse_candidate"
        noise = analysis_for("03a", ["q1"])
        noise["03a_stage"] = "refinement_candidate"
        noise["dataset_metrics"]["qubits"]["q1"]["robust_snr"] = 6.68
        rows = [
            {
                "status": "completed",
                "analysis_status": "needs_review",
                "analysis_json": json.dumps(candidate),
                "parameters_json": json.dumps(
                    {"qubits": ["q1"], "num_averages": 500}
                ),
                "decision": "repeat",
            },
            {
                "status": "completed",
                "analysis_status": "needs_review",
                "analysis_json": json.dumps(noise),
                "parameters_json": json.dumps(
                    {"qubits": ["q1"], "num_averages": 2000}
                ),
                "decision": "manual_review",
            },
        ]
        self.assertEqual(resolve_03a_candidate_targets(rows), set())
        self.assertEqual(resolve_03a_shift_ready_targets(rows), {"q1"})

        recovered = analysis_for("03a", ["q1"])
        recovered["03a_stage"] = "coarse_candidate"
        rows.append(
            {
                "status": "completed",
                "analysis_status": "pass",
                "analysis_json": json.dumps(recovered),
                "parameters_json": json.dumps(
                    {"qubits": ["q1"], "num_averages": 500}
                ),
                "decision": "repeat",
            }
        )
        self.assertEqual(resolve_03a_candidate_targets(rows), {"q1"})

    def test_03a_subgroups_accumulate_before_advance(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state, wiring, all_targets = sample_multiplex_state_and_wiring()
            targets = all_targets[:3]
            for name in targets:
                state["qubits"][name]["xy"]["intermediate_frequency"] = 0.0
            settings = make_settings(Path(folder), state)
            atomic_write_json(settings.wiring_path, wiring)
            service = AgentService(settings)
            workflow = service.start_workflow(
                targets,
                {"multiplexed": True},
                "unittest",
                ENTRY_PHRASE,
            )
            service.db.execute(
                "UPDATE workflows SET current_node = '03a' WHERE id = ?",
                (workflow["id"],),
            )

            initial = service.request_run(
                workflow["id"],
                "03a",
                {
                    "qubits": targets,
                    "frequency_span_in_mhz": 800.0,
                    "operation_amplitude_factor": 0.10,
                },
                "Initial all-target 03a coarse search.",
                "unittest",
            )
            self._insert_run(
                service,
                workflow["id"],
                initial["id"],
                "offline-03a-initial",
                "03a",
                targets,
                stage_03a="coarse_candidate",
            )

            refinement = service.request_run(
                workflow["id"],
                "03a",
                {
                    "qubits": [targets[0]],
                    "frequency_span_in_mhz": 100.0,
                    "operation_amplitude_factor": 0.075,
                },
                "Refine a coarse candidate at intermediate power.",
                "unittest",
            )
            self.assertEqual(
                refinement["payload"]["parameters"]["qubits"], [targets[0]]
            )

            first = service.request_run(
                workflow["id"],
                "03a",
                {
                    "qubits": [targets[0]],
                    "frequency_span_in_mhz": 50.0,
                    "operation_amplitude_factor": 0.02,
                },
                "Measure the first spectroscopy subgroup.",
                "unittest",
            )
            first_run = "offline-03a-first"
            self._insert_run(
                service,
                workflow["id"],
                first["id"],
                first_run,
                "03a",
                [targets[0]],
            )
            with self.assertRaisesRegex(Exception, "Cannot advance from 03a"):
                service.record_decision(
                    workflow["id"],
                    first_run,
                    "advance",
                    "Only one subgroup is resolved.",
                    "04",
                    {},
                    [],
                    "unittest",
                )

            second = service.request_run(
                workflow["id"],
                "03a",
                {
                    "qubits": targets[1:],
                    "frequency_span_in_mhz": 50.0,
                    "operation_amplitude_factor": 0.02,
                },
                "Measure the remaining spectroscopy subgroup.",
                "unittest",
            )
            second_run = "offline-03a-second"
            self._insert_run(
                service,
                workflow["id"],
                second["id"],
                second_run,
                "03a",
                targets[1:],
            )
            result = service.record_decision(
                workflow["id"],
                second_run,
                "advance",
                "All active targets now have spectroscopy evidence.",
                "04",
                {},
                [],
                "unittest",
            )
            self.assertEqual(result["next_action"]["next_node"], "04")

    def test_04_accepts_a_multiplexed_subgroup(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state, wiring, all_targets = sample_multiplex_state_and_wiring()
            targets = all_targets[:3]
            settings = make_settings(Path(folder), state)
            atomic_write_json(settings.wiring_path, wiring)
            service = AgentService(settings)
            workflow = service.start_workflow(
                targets,
                {"multiplexed": True},
                "unittest",
                ENTRY_PHRASE,
            )
            service.db.execute(
                "UPDATE workflows SET current_node = '04' WHERE id = ?",
                (workflow["id"],),
            )
            proposal = service.request_run(
                workflow["id"],
                "04",
                {"qubits": targets[1:]},
                "Measure only unresolved Power-Rabi targets.",
                "unittest",
            )
            self.assertEqual(
                proposal["payload"]["parameters"]["qubits"], targets[1:]
            )
            self.assertTrue(proposal["payload"]["parameters"]["multiplexed"])

    def test_05_accepts_a_multiplexed_subgroup(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state, wiring, all_targets = sample_multiplex_state_and_wiring()
            targets = all_targets[:3]
            settings = make_settings(Path(folder), state)
            atomic_write_json(settings.wiring_path, wiring)
            service = AgentService(settings)
            workflow = service.start_workflow(
                targets,
                {"multiplexed": True},
                "unittest",
                ENTRY_PHRASE,
            )
            service.db.execute(
                "UPDATE workflows SET current_node = '05' WHERE id = ?",
                (workflow["id"],),
            )
            proposal = service.request_run(
                workflow["id"],
                "05",
                {"qubits": [targets[0]]},
                "Isolate one T1 trace so another qubit cannot abort its fit.",
                "unittest",
            )
            self.assertEqual(
                proposal["payload"]["parameters"]["qubits"], [targets[0]]
            )
            self.assertTrue(proposal["payload"]["parameters"]["multiplexed"])

    def test_05_resolution_is_per_qubit(self) -> None:
        analysis = analysis_for("05", ["q1", "q2"])
        analysis["fit_quality"]["results"]["q2"]["r_squared"] = 0.5
        rows = [
            {
                "status": "completed",
                "analysis_status": "needs_review",
                "parameters_json": json.dumps({"qubits": ["q1", "q2"]}),
                "analysis_json": json.dumps(analysis),
            }
        ]
        self.assertEqual(resolve_node_targets("05", rows), {"q1"})

    def test_04_and_05_require_reference_quality_snr(self) -> None:
        rabi = analysis_for("04", ["q1", "q3"])
        rabi["dataset_metrics"]["qubits"]["q1"]["robust_snr"] = 13.9
        rabi["dataset_metrics"]["qubits"]["q3"]["robust_snr"] = 14.9
        t1 = analysis_for("05", ["q1", "q3"])
        t1["dataset_metrics"]["qubits"]["q1"]["robust_snr"] = 24.9
        t1["dataset_metrics"]["qubits"]["q3"]["robust_snr"] = 25.1
        self.assertEqual(
            resolve_node_targets(
                "04",
                [{"analysis_status": "needs_review", "analysis_json": json.dumps(rabi)}],
            ),
            {"q3"},
        )
        self.assertEqual(
            resolve_node_targets(
                "05",
                [{"analysis_status": "needs_review", "analysis_json": json.dumps(t1)}],
            ),
            {"q3"},
        )

    def test_new_lease_reopens_advanced_04_when_snr_policy_regresses(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state, _wiring, targets = sample_multiplex_state_and_wiring()
            settings = make_settings(Path(folder), state)
            service = AgentService(settings)
            target = targets[0]
            workflow = start_test_workflow(service, [target])
            service.db.execute(
                "UPDATE workflows SET current_node = '05' WHERE id = ?",
                (workflow["id"],),
            )
            proposal = service._create_proposal(
                workflow["id"], "run", {}, "unittest", 60, 1
            )
            analysis = analysis_for("04", [target])
            analysis["dataset_metrics"]["qubits"][target]["robust_snr"] = 13.0
            service.db.execute(
                """
                INSERT INTO runs(
                    id, workflow_id, proposal_id, node_id, parameters_json,
                    status, analysis_status, analysis_json
                ) VALUES ('old-04', ?, ?, '04', ?, 'completed', 'pass', ?)
                """,
                (
                    workflow["id"],
                    proposal["id"],
                    json.dumps({"qubits": [target]}),
                    json.dumps(analysis),
                ),
            )
            service.db.execute(
                """
                INSERT INTO decisions(
                    id, workflow_id, run_id, decision, reason, next_node,
                    next_parameters_json, state_patch_json, client_id, created_at
                ) VALUES ('old-04-advance', ?, 'old-04', 'advance',
                          'Accepted under an older rule.', '05', '{}', '[]',
                          'unittest', '2026-08-19T00:00:00+00:00')
                """,
                (workflow["id"],),
            )
            lease = service.request_autonomy_lease(
                workflow["id"],
                "unittest",
                service.autonomy_mode_entry_phrase,
                "Revalidate old evidence before continuing.",
            )
            self.assertEqual(lease["payload"]["allowed_nodes"][0], "04")
            self.assertEqual(service.status(workflow["id"])["workflow"]["current_node"], "04")
            event = service.db.one(
                "SELECT payload_json FROM events "
                "WHERE event_type = 'workflow_evidence_revalidation' "
                "ORDER BY id DESC LIMIT 1"
            )
            self.assertIn(target, json.loads(event["payload_json"])["missing_targets"])
    @staticmethod
    def _insert_run(
        service: AgentService,
        workflow_id: str,
        proposal_id: str,
        run_id: str,
        node_id: str,
        targets: list[str],
        stage_03a: str = "fine",
    ) -> None:
        analysis = analysis_for(node_id, targets)
        if node_id == "03a":
            analysis["03a_stage"] = stage_03a
        service.db.execute(
            """
            INSERT INTO runs(
                id, workflow_id, proposal_id, node_id, parameters_json,
                status, analysis_status, analysis_json
            ) VALUES (?, ?, ?, ?, ?, 'completed', 'pass', ?)
            """,
            (
                run_id,
                workflow_id,
                proposal_id,
                node_id,
                json.dumps(
                    {
                        "qubits": targets,
                        "frequency_span_in_mhz": (
                            800.0 if stage_03a == "coarse_candidate" else 50.0
                        ),
                        "operation_amplitude_factor": (
                            0.10 if stage_03a == "coarse_candidate" else 0.02
                        ),
                    }
                ),
                json.dumps(analysis),
            ),
        )


if __name__ == "__main__":
    unittest.main()
