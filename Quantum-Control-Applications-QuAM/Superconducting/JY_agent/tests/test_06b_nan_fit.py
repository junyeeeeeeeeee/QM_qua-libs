from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from jy_agent.analysis import _attribute_failures, _drop_missing_values
from jy_agent.util import atomic_write_json, json_compatible


class NanNodeFitTests(unittest.TestCase):
    """06b run 2026-10-02: the node's fit_decay_exp recorded T2echo = NaN for
    rising traces, and the strict JSON write crashed the worker for every
    target."""

    def test_non_finite_floats_become_none(self) -> None:
        updates = {
            "#/qubits/q1/T2echo": {"key": "#/qubits/q1/T2echo", "new": float("nan")},
            "#/qubits/q2/T2echo": {"key": "#/qubits/q2/T2echo", "new": np.float64(9.9e-6)},
            "#/qubits/q5/T2echo": {"key": "#/qubits/q5/T2echo", "new": np.float64("inf")},
        }
        result = json_compatible(updates)
        self.assertIsNone(result["#/qubits/q1/T2echo"]["new"])
        self.assertEqual(result["#/qubits/q2/T2echo"]["new"], 9.9e-6)
        self.assertIsNone(result["#/qubits/q5/T2echo"]["new"])
        with tempfile.TemporaryDirectory() as folder:
            atomic_write_json(Path(folder) / "updates.json", result)

    def test_missing_value_fails_only_that_qubit(self) -> None:
        current = {"qubits": {"q1": {"T2echo": 1e-5}, "q2": {"T2echo": 1e-5}}}
        patch = [
            {"op": "replace", "path": "/qubits/q1/T2echo", "value": None},
            {"op": "replace", "path": "/qubits/q2/T2echo", "value": 9.9e-6},
        ]
        kept, failures = _drop_missing_values(patch, current)
        self.assertEqual([item["path"] for item in kept], ["/qubits/q2/T2echo"])
        by_target, run_level = _attribute_failures(failures, ["q1", "q2"])
        self.assertEqual(list(by_target), ["q1"])
        self.assertEqual(run_level, [])

    def test_restating_an_existing_none_is_kept(self) -> None:
        current = {"qubits": {"q1": {"T2echo": None}}}
        patch = [{"op": "replace", "path": "/qubits/q1/T2echo", "value": None}]
        kept, failures = _drop_missing_values(patch, current)
        self.assertEqual(kept, patch)
        self.assertEqual(failures, [])

    def test_nan_value_is_dropped(self) -> None:
        current = {"qubits": {"q1": {}}}
        patch = [{"op": "add", "path": "/qubits/q1/T2echo", "value": math.nan}]
        kept, failures = _drop_missing_values(patch, current)
        self.assertEqual(kept, [])
        self.assertTrue(failures[0].startswith("q1 "))


class PerTargetPolicyTests(unittest.TestCase):
    """2026-10-04: q10's negative T2ramsey suppressed every 06 target."""

    def test_one_invalid_value_fails_only_its_qubit(self) -> None:
        from jy_agent.analysis import _validate_patch_per_target
        from jy_agent.policy import PolicyEngine
        from tests.test_core import make_settings, sample_state

        with tempfile.TemporaryDirectory() as folder:
            state = sample_state(0.2, 0.1)
            state["qubits"]["q2"] = json.loads(json.dumps(state["qubits"]["q1"]))
            settings = make_settings(Path(folder), state)
            patch = [
                {"op": "add", "path": "/qubits/q1/T2ramsey", "value": -1.9e-4},
                {"op": "add", "path": "/qubits/q2/T2ramsey", "value": 9.2e-6},
            ]
            kept, failures = _validate_patch_per_target(
                PolicyEngine(settings), patch, state
            )
            self.assertEqual([item["path"] for item in kept], ["/qubits/q2/T2ramsey"])
            self.assertEqual(len(failures), 1)
            by_target, run_level = _attribute_failures(failures, ["q1", "q2"])
            self.assertEqual(list(by_target), ["q1"])
            self.assertEqual(run_level, [])


class WithheldTargetTests(unittest.TestCase):
    """2026-10-04: q9/q10 passed 06 analysis but were left out of the commit."""

    def test_withheld_until_a_later_run_commits_it(self) -> None:
        from jy_agent.service import _withheld_targets

        def row(passing, patch_targets):
            patch = [
                {"op": "replace", "path": f"/qubits/{name}/T2ramsey", "value": 1e-5}
                for name in patch_targets
            ]
            return {
                "analysis_json": json.dumps({"passing_targets": passing}),
                "state_patch_json": json.dumps(patch),
            }

        rows = [
            row(["q1", "q3", "q9"], ["q1", "q3"]),  # q9 withheld
            row(["q8", "q10"], ["q8"]),  # q10 withheld
        ]
        self.assertEqual(_withheld_targets(rows), {"q9", "q10"})
        rows.append(row(["q9"], ["q9"]))  # a later run commits q9
        self.assertEqual(_withheld_targets(rows), {"q10"})
        # A decision with no patch withholds nothing.
        self.assertEqual(_withheld_targets([row(["q2"], [])]), set())


if __name__ == "__main__":
    unittest.main()
