from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jy_agent.analysis import SnapshotAnalyzer
from jy_agent.policy import PolicyEngine
from jy_agent.workflow_subgroups import resolve_node_targets

from test_core import make_settings, sample_state


class StatisticsAnalysisTests(unittest.TestCase):
    def _analyzer(self, folder: str) -> SnapshotAnalyzer:
        settings = make_settings(Path(folder), sample_state(0.2, 0.1))
        return SnapshotAnalyzer(settings, PolicyEngine(settings))

    def test_statistics_metadata_does_not_require_or_refit_raw_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            analyzer = self._analyzer(folder)
            metrics = analyzer._dataset_metrics(
                Path(folder) / "snapshot-without-ds",
                "05st",
                {
                    "initial_parameters": {"histo_num": 100},
                    "t1_stats": {"q1": {"mu_us": 40.0, "sigma_us": 0.54}},
                },
            )

            self.assertNotIn("error", metrics)
            self.assertEqual(metrics["validation_mode"], "baseline_prerequisites_only")
            self.assertFalse(metrics["raw_iteration_refit_required"])
            self.assertEqual(
                metrics["protected_node_summaries"]["q1"]["mu_us"], 40.0
            )

    def test_one_hundred_repetition_run_completes_without_secondary_fit_gate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            analyzer = self._analyzer(folder)
            result = analyzer._fit_quality(
                "06st_t2star",
                {
                    "initial_parameters": {"histo_num": 100},
                    # Informational output is not re-fitted and cannot invalidate
                    # a completed statistics run.
                    "t2*_stats": {"q1": {"mu_us": None, "sigma_us": None}},
                },
                {
                    "validation_mode": "baseline_prerequisites_only",
                    "raw_iteration_refit_required": False,
                },
                ["q1"],
            )

            self.assertEqual(result["failures"], [])
            self.assertEqual(result["results"]["q1"]["histo_num"], 100)
            self.assertTrue(result["results"]["q1"]["fit_successful"])
            self.assertFalse(
                result["results"]["q1"]["raw_iteration_refit_required"]
            )

    def test_statistics_still_requires_exactly_one_hundred_repetitions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            analyzer = self._analyzer(folder)
            result = analyzer._fit_quality(
                "06st_t2e",
                {"initial_parameters": {"histo_num": 99}},
                {},
                ["q1"],
            )

            self.assertFalse(result["results"]["q1"]["fit_successful"])
            self.assertIn("exactly 100 repetitions", "\n".join(result["failures"]))

    def test_single_completed_statistics_run_resolves_its_target(self) -> None:
        analysis = {
            "fit_quality": {
                "results": {
                    "q1": {
                        "fit_successful": True,
                        "histo_num": 100,
                        "raw_iteration_refit_required": False,
                    }
                }
            },
            "dataset_metrics": {"qubits": {}},
        }
        resolved = resolve_node_targets(
            "05st",
            [
                {
                    "status": "completed",
                    "analysis_status": "pass",
                    "analysis_json": json.dumps(analysis),
                    "parameters_json": json.dumps({"qubits": ["q1"]}),
                    "decision": "advance",
                }
            ],
        )

        self.assertEqual(resolved, {"q1"})


if __name__ == "__main__":
    unittest.main()
