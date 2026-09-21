"""Deterministic next-action planning and the compact evidence digest.

Operator instruction 2026-09-20. The long-term aim is that a cheap model can
drive the bring-up loop by following the rulebook, so the decision it has to
make must be a small one: the server says which node, which qubits, and which
parameters differ from the node defaults, and cites the rule. Where no rule is
registered it says so instead of inventing a parameter.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jy_agent.analysis import evidence_digest
from jy_agent.service import AgentService
from jy_agent.util import json_dumps

from test_autonomy import activate_lease
from test_core import make_settings, sample_state, start_test_workflow


def analysis_document(
    node_id: str, snr_by_qubit: dict[str, float], status: str = "needs_review"
) -> dict:
    passing = [name for name, snr in snr_by_qubit.items() if snr >= 999]
    return {
        "analysis_status": status,
        "node_id": node_id,
        "plots": ["/tmp/plot.png"],
        "passing_targets": passing,
        "failure_reasons_by_target": {
            name: [f"{name} robust sweep SNR {snr:g} is below the floor."]
            for name, snr in snr_by_qubit.items()
            if snr < 999
        },
        "run_level_failure_reasons": [],
        "dataset_metrics": {
            "qubits": {
                name: {
                    "robust_snr": snr,
                    "edge_fraction": 0.4,
                    "sweep_min_coordinate": 0.0,
                    "sweep_max_coordinate": 1.9,
                    "raw_extremum_edge_fraction": 0.4,
                }
                for name, snr in snr_by_qubit.items()
            }
        },
        "fit_quality": {
            "results": {
                name: {"fit_successful": True, "Pi_amplitude": 0.05}
                for name in snr_by_qubit
            }
        },
        "warnings": ["a warning that the digest should drop"],
        "candidate_state_patch": [],
    }


class NextActionTests(unittest.TestCase):
    def build(self, folder: str, targets: list[str]) -> tuple[AgentService, dict, dict]:
        state = sample_state(0.2, 0.1)
        for name in targets:
            if name not in state["qubits"]:
                state["qubits"][name] = json.loads(
                    json.dumps(state["qubits"]["q1"])
                )
        state["active_qubit_names"] = targets
        service = AgentService(make_settings(Path(folder), state))
        workflow = start_test_workflow(service, targets)
        proposal, lease = activate_lease(service, workflow["id"])
        return service, workflow, proposal

    def test_first_run_of_a_node_asks_for_every_active_target(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, _proposal = self.build(folder, ["q1", "q2"])

            plan = service.next_action(workflow["id"])

            self.assertEqual(plan["action"], "run")
            self.assertEqual(plan["current_node"], "02x")
            self.assertEqual(plan["run"]["qubits"], ["q1", "q2"])
            self.assertEqual(plan["targets"]["unresolved"], ["q1", "q2"])
            self.assertIn("multiplex", plan["reason"])
            self.assertIn("PLAYBOOK: Multiplex workflows", plan["rules"])

    def test_a_running_worker_means_wait(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, proposal = self.build(folder, ["q1"])
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status) VALUES "
                "('in-flight', ?, ?, '02x', ?, 'running')",
                (workflow["id"], proposal["id"], json_dumps({"qubits": ["q1"]})),
            )

            plan = service.next_action(workflow["id"])

            self.assertEqual(plan["action"], "wait")
            self.assertIn("in-flight", plan["reason"])

    def test_a_completed_unanalyzed_run_means_analyze(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, proposal = self.build(folder, ["q1"])
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status) VALUES "
                "('done', ?, ?, '02x', ?, 'completed')",
                (workflow["id"], proposal["id"], json_dumps({"qubits": ["q1"]})),
            )

            plan = service.next_action(workflow["id"])

            self.assertEqual(plan["action"], "analyze")

    def test_an_analyzed_run_without_a_decision_means_record_decision(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, proposal = self.build(folder, ["q1"])
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status, analysis_status, analysis_json) VALUES "
                "('analyzed', ?, ?, '02x', ?, 'completed', 'needs_review', ?)",
                (
                    workflow["id"],
                    proposal["id"],
                    json_dumps({"qubits": ["q1"]}),
                    json_dumps(analysis_document("02x", {"q1": 1.2})),
                ),
            )

            plan = service.next_action(workflow["id"])

            self.assertEqual(plan["action"], "record_decision")
            self.assertTrue(
                any("finishes a node" in rule for rule in plan["rules"])
            )

    def test_the_averaging_ladder_is_offered_for_a_noisy_04_retry(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, proposal = self.build(folder, ["q1", "q2"])
            service.db.execute(
                "UPDATE workflows SET current_node = '04' WHERE id = ?",
                (workflow["id"],),
            )
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status, analysis_status, analysis_json) VALUES "
                "('noisy-04', ?, ?, '04', ?, 'completed', 'needs_review', ?)",
                (
                    workflow["id"],
                    proposal["id"],
                    json_dumps({"qubits": ["q1", "q2"], "num_averages": 500}),
                    json_dumps(analysis_document("04", {"q1": 2.0, "q2": 3.0})),
                ),
            )
            service.db.execute(
                "INSERT INTO decisions(id, workflow_id, run_id, decision, reason, "
                "next_node, next_parameters_json, state_patch_json, client_id, "
                "created_at) VALUES ('d-04', ?, 'noisy-04', 'repeat', 'Noisy.', "
                "'04', '{}', '[]', 'unittest', '2026-09-20T00:00:00+00:00')",
                (workflow["id"],),
            )

            plan = service.next_action(workflow["id"])

            self.assertEqual(plan["action"], "run")
            self.assertEqual(plan["run"]["node_id"], "04")
            self.assertEqual(plan["run"]["parameters"]["num_averages"], 1000)
            self.assertEqual(plan["run"]["qubits"], ["q1", "q2"])
            self.assertTrue(
                any("noise_confirmation_num_averages" in rule for rule in plan["rules"])
            )
            self.assertEqual(plan["notes"], [])

    def test_no_registered_rule_says_so_instead_of_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, proposal = self.build(folder, ["q1", "q2"])
            service.db.execute(
                "UPDATE workflows SET current_node = '02c' WHERE id = ?",
                (workflow["id"],),
            )
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status, analysis_status, analysis_json) VALUES "
                "('ambiguous-02c', ?, ?, '02c', ?, 'completed', 'needs_review', ?)",
                (
                    workflow["id"],
                    proposal["id"],
                    json_dumps({"qubits": ["q1", "q2"]}),
                    json_dumps(analysis_document("02c", {"q1": 2.0, "q2": 3.0})),
                ),
            )
            service.db.execute(
                "INSERT INTO decisions(id, workflow_id, run_id, decision, reason, "
                "next_node, next_parameters_json, state_patch_json, client_id, "
                "created_at) VALUES ('d-02c', ?, 'ambiguous-02c', 'repeat', "
                "'Ambiguous.', '02c', '{}', '[]', 'unittest', "
                "'2026-09-20T00:00:00+00:00')",
                (workflow["id"],),
            )

            plan = service.next_action(workflow["id"])

            self.assertEqual(plan["action"], "run")
            self.assertEqual(plan["run"]["parameters"], {"qubits": ["q1", "q2"]})
            self.assertTrue(
                any("No registered deterministic" in note for note in plan["notes"])
            )

    def test_attempts_remaining_are_reported_per_target(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, proposal = self.build(folder, ["q1", "q2"])
            lease = service.db.one(
                "SELECT * FROM autonomy_leases WHERE workflow_id = ?",
                (workflow["id"],),
            )
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status, autonomy_lease_id) VALUES "
                "('attempt-1', ?, ?, '02x', ?, 'failed', ?)",
                (
                    workflow["id"],
                    proposal["id"],
                    json_dumps({"qubits": ["q1"]}),
                    lease["id"],
                ),
            )

            plan = service.next_action(workflow["id"])

            maximum = plan["attempts"]["maximum"]
            self.assertEqual(plan["attempts"]["used"]["q1"], 1)
            self.assertEqual(plan["attempts"]["used"]["q2"], 0)
            self.assertEqual(plan["attempts"]["remaining"]["q1"], maximum - 1)

    def test_03a_first_run_requires_the_zero_if_setup(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, _proposal = self.build(folder, ["q1"])
            service.db.execute(
                "UPDATE workflows SET current_node = '03a' WHERE id = ?",
                (workflow["id"],),
            )

            plan = service.next_action(workflow["id"])

            self.assertEqual(plan["action"], "setup")
            self.assertEqual(
                [item["tool"] for item in plan["required_setup"]],
                ["jy_request_initial_03a_zero_if"],
            )

    def test_bootstrap_is_required_when_x180_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state = sample_state(0.0, 0.0)
            service = AgentService(make_settings(Path(folder), state))
            workflow = start_test_workflow(service, ["q1"])
            activate_lease(service, workflow["id"])
            service.db.execute(
                "UPDATE workflows SET current_node = '04' WHERE id = ?",
                (workflow["id"],),
            )

            plan = service.next_action(workflow["id"])

            self.assertEqual(plan["action"], "setup")
            self.assertIn(
                "jy_request_bootstrap",
                [item["tool"] for item in plan["required_setup"]],
            )


class EvidenceDigestTests(unittest.TestCase):
    def test_digest_keeps_gated_numbers_and_drops_the_rest(self) -> None:
        digest = evidence_digest(analysis_document("04", {"q1": 2.0, "q2": 30.0}))

        self.assertEqual(digest["analysis_status"], "needs_review")
        self.assertEqual(digest["node_id"], "04")
        self.assertEqual(digest["qubits"]["q1"]["robust_snr"], 2.0)
        self.assertEqual(digest["qubits"]["q1"]["edge_fraction"], 0.4)
        self.assertEqual(digest["qubits"]["q1"]["Pi_amplitude"], 0.05)
        self.assertNotIn("warnings", digest)
        self.assertNotIn("candidate_state_patch", digest)
        self.assertNotIn(
            "sweep_min_coordinate", digest["qubits"]["q1"]
        )

    def test_digest_of_a_missing_analysis_is_empty(self) -> None:
        self.assertEqual(evidence_digest(None), {})

    def test_digest_tolerates_null_per_qubit_sections(self) -> None:
        # 02x records `"fit_quality": {"results": null}` because it has no
        # per-qubit fitter; iterating that null used to raise TypeError and
        # took down every run request that carried prior experience.
        digest = evidence_digest(
            {
                "analysis_status": "pass",
                "fit_quality": {"results": None},
                "dataset_metrics": {"qubits": {"q1": {"robust_snr": 12.5}}},
            }
        )
        self.assertEqual(digest["qubits"]["q1"]["robust_snr"], 12.5)
        self.assertEqual(
            evidence_digest(
                {"fit_quality": {"results": None}, "dataset_metrics": {"qubits": None}}
            ).get("qubits", {}),
            {},
        )

    def test_experience_entries_carry_the_digest_not_the_document(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(
                make_settings(Path(folder), sample_state(0.2, 0.1))
            )
            workflow = start_test_workflow(service, ["q1"])
            proposal = service._create_proposal(
                workflow["id"], "run", {}, "unittest", 60, 1
            )
            service.db.execute(
                "INSERT INTO runs(id, workflow_id, proposal_id, node_id, "
                "parameters_json, status, analysis_status, analysis_json) VALUES "
                "('experience-run', ?, ?, '04', ?, 'completed', 'needs_review', ?)",
                (
                    workflow["id"],
                    proposal["id"],
                    json_dumps({"qubits": ["q1"]}),
                    json_dumps(analysis_document("04", {"q1": 2.0})),
                ),
            )
            service.db.execute(
                "INSERT INTO decisions(id, workflow_id, run_id, decision, reason, "
                "next_node, next_parameters_json, state_patch_json, client_id, "
                "created_at) VALUES ('d-exp', ?, 'experience-run', 'repeat', "
                "'Noisy.', '04', '{}', '[]', 'unittest', "
                "'2026-09-20T00:00:00+00:00')",
                (workflow["id"],),
            )

            entries = service.decision_experience("04", ["q1"])
            self.assertEqual(len(entries), 1)
            self.assertNotIn("analysis", entries[0])
            self.assertEqual(entries[0]["evidence"]["qubits"]["q1"]["robust_snr"], 2.0)

            full = service.decision_experience("04", ["q1"], include_analysis=True)
            self.assertIn("analysis", full[0])
            self.assertIn("warnings", full[0]["analysis"])


if __name__ == "__main__":
    unittest.main()
