"""Partial-pass state commits.

Operator instruction 2026-09-20. A run whose overall ``analysis_status`` is
``needs_review`` used to suppress the state patch for every qubit in it,
including the ones that passed. Combined with the rule that refuses a further
run once all targets are resolved, calibrated values could go missing from
``state.json`` for good. These tests pin the per-target behaviour: passing
evidence commits, non-passing evidence never does.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import numpy as np
import xarray as xr

from jy_agent.analysis import SnapshotAnalyzer
from jy_agent.policy import PolicyEngine
from jy_agent.service import AgentService, AutonomyEvidenceError
from jy_agent.state import patch_qubit_targets, split_patch_by_target
from jy_agent.util import atomic_write_json, json_dumps, sha256_file

from test_autonomy import activate_lease
from test_core import make_settings, sample_state, start_test_workflow


def two_qubit_state() -> dict:
    """A state with q1 and q2 so one target can pass while the other fails."""

    state = sample_state(0.2, 0.1)
    state["qubits"]["q2"] = deepcopy(state["qubits"]["q1"])
    state["qubits"]["q2"]["resonator"]["intermediate_frequency"] = -20_000_000
    state["active_qubit_names"] = ["q1", "q2"]
    return state


def write_05_snapshot(snapshot: Path, *, q2_is_noise: bool, save_plot: bool) -> None:
    """One multiplexed T1 snapshot: q1 always decays, q2 optionally does not."""

    snapshot.mkdir()
    atomic_write_json(
        snapshot / "node.json",
        {"id": 705, "data": {"outcomes": {"q1": "successful", "q2": "successful"}}},
    )
    atomic_write_json(
        snapshot / "data.json",
        {"ds": "./ds.h5", "initial_parameters": {"qubits": ["q1", "q2"]}},
    )
    if save_plot:
        (snapshot / "figure.png").write_bytes(b"synthetic-t1-plot")
    idle_time = np.linspace(0.016, 200.0, 121)
    clean = 0.04 + 0.35 * np.exp(-idle_time / 35.0)
    clean += 0.00005 * np.sin(np.arange(idle_time.size))
    if q2_is_noise:
        second = 0.04 + 0.02 * np.sin(np.arange(idle_time.size) * 1.7)
    else:
        second = 0.04 + 0.35 * np.exp(-idle_time / 45.0)
        second += 0.00005 * np.sin(np.arange(idle_time.size))
    signal = np.vstack([clean, second])
    dataset = xr.Dataset(
        {"I": (("qubit", "idle_time"), signal)},
        coords={"qubit": ["q1", "q2"], "idle_time": idle_time},
    )
    dataset.idle_time.attrs["units"] = "us"
    dataset.to_netcdf(snapshot / "ds.h5", engine="scipy")
    proposed = two_qubit_state()
    proposed["qubits"]["q1"]["T1"] = 35e-6
    proposed["qubits"]["q2"]["T1"] = 45e-6
    atomic_write_json(snapshot / "quam_state.json", proposed)


class PatchTargetHelperTests(unittest.TestCase):
    def test_patch_qubit_targets_reads_every_qubit_pointer(self) -> None:
        patch = [
            {"op": "add", "path": "/qubits/q1/T1", "value": 1.0},
            {"op": "replace", "path": "/qubits/q7/resonator/confusion_matrix", "value": []},
            {"op": "replace", "path": "/ports/mw_outputs/con1/1/1/upconverter_frequency", "value": 1},
        ]
        self.assertEqual(patch_qubit_targets(patch), {"q1", "q7"})

    def test_split_patch_keeps_only_allowed_qubits(self) -> None:
        patch = [
            {"op": "add", "path": "/qubits/q1/T1", "value": 1.0},
            {"op": "add", "path": "/qubits/q2/T1", "value": 2.0},
            {"op": "replace", "path": "/ports/mw_outputs/con1/1/1/upconverter_frequency", "value": 1},
        ]
        kept, dropped = split_patch_by_target(patch, {"q1"})
        self.assertEqual([item["path"] for item in kept], ["/qubits/q1/T1"])
        self.assertEqual(
            [item["path"] for item in dropped],
            ["/qubits/q2/T1", "/ports/mw_outputs/con1/1/1/upconverter_frequency"],
        )


class PartialPassAnalysisTests(unittest.TestCase):
    def analyze(self, root: Path, *, q2_is_noise: bool, save_plot: bool = True) -> dict:
        settings = make_settings(root / "settings", two_qubit_state())
        snapshot = root / "snapshot"
        write_05_snapshot(snapshot, q2_is_noise=q2_is_noise, save_plot=save_plot)
        run = {
            "node_id": "05",
            "parameters_json": json.dumps({"qubits": ["q1", "q2"]}),
            "active_state_hash_before": sha256_file(settings.active_state),
        }
        analyzer = SnapshotAnalyzer(settings, PolicyEngine(settings))
        return analyzer.analyze_snapshot(snapshot, "05", run)

    def test_passing_qubit_still_commits_inside_a_needs_review_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            result = self.analyze(Path(folder), q2_is_noise=True)

            self.assertEqual(result["analysis_status"], "needs_review")
            self.assertEqual(result["passing_targets"], ["q1"])
            self.assertEqual(result["run_level_failure_reasons"], [])
            self.assertIn("q2", result["failure_reasons_by_target"])
            self.assertNotIn("q1", result["failure_reasons_by_target"])
            self.assertEqual(
                patch_qubit_targets(result["candidate_state_patch"]), {"q1"}
            )

    def test_every_qubit_commits_when_the_whole_run_passes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            result = self.analyze(Path(folder), q2_is_noise=False)

            self.assertEqual(result["analysis_status"], "pass")
            self.assertEqual(result["passing_targets"], ["q1", "q2"])
            self.assertEqual(result["suppressed_patch_targets"], [])
            self.assertEqual(
                patch_qubit_targets(result["candidate_state_patch"]), {"q1", "q2"}
            )

    def test_run_level_failure_still_suppresses_every_target(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            result = self.analyze(Path(folder), q2_is_noise=False, save_plot=False)

            self.assertEqual(result["analysis_status"], "needs_review")
            self.assertEqual(result["passing_targets"], [])
            self.assertIn(
                "No result plot was saved.", result["run_level_failure_reasons"]
            )
            self.assertEqual(result["candidate_state_patch"], [])


class PartialPassCommitAuthorizationTests(unittest.TestCase):
    def build(self, root: Path, passing_targets: list[str]) -> tuple[AgentService, dict, dict]:
        service = AgentService(make_settings(root, two_qubit_state()))
        workflow = start_test_workflow(service, ["q1", "q2"])
        proposal, lease = activate_lease(service, workflow["id"])
        snapshot = service.settings.data_root / "snapshot-partial"
        snapshot.mkdir()
        service.db.execute(
            """
            INSERT INTO runs(
                id, workflow_id, proposal_id, node_id, parameters_json,
                status, snapshot_id, snapshot_path, analysis_status,
                analysis_json, autonomy_lease_id
            ) VALUES ('partial-pass-run', ?, ?, '02x', ?, 'completed', 11, ?,
                      'needs_review', ?, ?)
            """,
            (
                workflow["id"],
                proposal["id"],
                json_dumps({"qubits": ["q1", "q2"]}),
                str(snapshot),
                json_dumps(
                    {
                        "analysis_status": "needs_review",
                        "passing_targets": passing_targets,
                    }
                ),
                lease["id"],
            ),
        )
        return service, workflow, lease

    @staticmethod
    def if_patch(name: str, value: int) -> list[dict]:
        return [
            {
                "op": "replace",
                "path": f"/qubits/{name}/xy/intermediate_frequency",
                "value": value,
            }
        ]

    def test_passing_target_commits_from_a_needs_review_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, lease = self.build(Path(folder), ["q1"])
            patch = self.if_patch("q1", 110_000_000)
            service.record_decision(
                workflow["id"], "partial-pass-run", "repeat",
                "q1 passed every check; q2 did not.", "02x", {}, patch,
                "unittest-agent",
            )

            result = service.commit_authorized_state(
                workflow["id"], lease["id"], patch,
                "Commit only the target that passed.", "unittest-agent",
                "partial-pass-run",
            )

            self.assertEqual(result["lease_id"], lease["id"])
            committed = json.loads(
                service.settings.active_state.read_text(encoding="utf-8")
            )
            self.assertEqual(
                committed["qubits"]["q1"]["xy"]["intermediate_frequency"],
                110_000_000,
            )
            self.assertEqual(
                service.autonomy_status(lease_id=lease["id"])["status"], "active"
            )

    def test_non_passing_target_is_refused_without_halting_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, lease = self.build(Path(folder), ["q1"])
            patch = self.if_patch("q2", 110_000_000)
            service.record_decision(
                workflow["id"], "partial-pass-run", "repeat",
                "q2 did not pass.", "02x", {}, patch, "unittest-agent",
            )

            with self.assertRaises(AutonomyEvidenceError):
                service.commit_authorized_state(
                    workflow["id"], lease["id"], patch,
                    "This must not be committed.", "unittest-agent",
                    "partial-pass-run",
                )

            self.assertEqual(
                service.autonomy_status(lease_id=lease["id"])["status"], "active"
            )

    def test_analysis_without_per_target_evidence_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, workflow, lease = self.build(Path(folder), ["q1"])
            service.db.execute(
                "UPDATE runs SET analysis_json = ? WHERE id = 'partial-pass-run'",
                (json_dumps({"analysis_status": "needs_review"}),),
            )
            patch = self.if_patch("q1", 110_000_000)
            service.record_decision(
                workflow["id"], "partial-pass-run", "repeat",
                "Legacy analysis.", "02x", {}, patch, "unittest-agent",
            )

            with self.assertRaises(AutonomyEvidenceError):
                service.commit_authorized_state(
                    workflow["id"], lease["id"], patch,
                    "Legacy analysis has no per-target evidence.",
                    "unittest-agent", "partial-pass-run",
                )

            self.assertEqual(
                service.autonomy_status(lease_id=lease["id"])["status"], "active"
            )


if __name__ == "__main__":
    unittest.main()
