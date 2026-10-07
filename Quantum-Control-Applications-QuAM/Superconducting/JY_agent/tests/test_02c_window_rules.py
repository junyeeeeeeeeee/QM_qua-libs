"""Operator rules 2026-10-05 for 02c: window 2-5x the bare-dressed separation,
a smeared dressed plateau blocks, and the power window keeps its width."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import xarray as xr

from jy_agent.analysis_02c import analyze_02c_transitions
from jy_agent.policy import PolicyEngine, PolicyError
from tests.test_core import make_settings, sample_state

RULES = {
    "min_window_to_separation_ratio": 2.0,
    "max_window_to_separation_ratio": 5.0,
    "require_dressed_plateau_quality": True,
}


def transition_dataset(
    span_hz: float, separation_hz: float, dressed_noise_hz: float = 0.0
) -> xr.Dataset:
    """Dressed line at -sep/2 for low power, bare at +sep/2 above -30 dBm."""
    power = np.linspace(-50, -10, 30)
    freq = np.linspace(-span_hz / 2, span_hz / 2, 201)
    trace = np.where(power < -30, -separation_hz / 2, separation_hz / 2)
    if dressed_noise_hz:
        rng = np.random.default_rng(3)
        low = power < -40
        trace = trace.astype(float)
        trace[low] = rng.uniform(-dressed_noise_hz, dressed_noise_hz, low.sum())
    return xr.Dataset(
        {"rr_min_response": (("qubit", "power_dbm"), trace[None, :])},
        coords={"qubit": ["q1"], "power_dbm": power, "freq": freq},
    )


def failures(dataset: xr.Dataset) -> list[str]:
    result = analyze_02c_transitions(dataset, RULES)["qubits"]["q1"]
    return result["validation_failures"]


class WindowRatioTests(unittest.TestCase):
    def test_window_within_two_to_five_times_passes(self) -> None:
        self.assertEqual(failures(transition_dataset(3e6, 1e6)), [])

    def test_window_too_wide_for_the_separation_fails(self) -> None:
        reasons = failures(transition_dataset(16e6, 1e6))
        self.assertTrue(any("narrow it" in reason for reason in reasons), reasons)

    def test_window_too_narrow_for_the_separation_fails(self) -> None:
        reasons = failures(transition_dataset(3e6, 2e6))
        self.assertTrue(any("widen it" in reason for reason in reasons), reasons)

    def test_recommended_span_and_midpoint_are_reported(self) -> None:
        result = analyze_02c_transitions(transition_dataset(16e6, 1e6), RULES)
        transition = result["qubits"]["q1"]
        self.assertAlmostEqual(transition["recommended_frequency_span_mhz"], 3.5, places=1)
        self.assertAlmostEqual(transition["midpoint_frequency_offset_hz"], 0.0, delta=1e5)


class DressedQualityTests(unittest.TestCase):
    def test_noise_band_on_the_dressed_side_blocks(self) -> None:
        reasons = failures(transition_dataset(3e6, 1e6, dressed_noise_hz=1.4e6))
        self.assertTrue(
            any("dressed plateau" in reason for reason in reasons), reasons
        )


class NeedsReviewResolutionTests(unittest.TestCase):
    """2026-10-05: per-target passes inside a needs_review 02c run resolve."""

    def test_passing_target_in_needs_review_run_is_resolved(self) -> None:
        import json

        from jy_agent.workflow_02c import resolve_02c_targets

        transition = {
            "validation_failures": [],
            "dressed_frequency_hz": 6.0e9,
            "dressed_power_limit_dbm": -30.0,
        }
        failing = {**transition, "validation_failures": ["window too wide"]}
        row = {
            "analysis_status": "needs_review",
            "analysis_json": json.dumps(
                {
                    "dataset_metrics": {
                        "qubits": {
                            "q4": {"02c_transition": transition},
                            "q1": {"02c_transition": failing},
                        }
                    }
                }
            ),
        }
        resolved, _ = resolve_02c_targets([row])
        self.assertEqual(resolved, {"q4"})
        self.assertEqual(
            resolve_02c_targets([{**row, "analysis_status": "failed"}])[0], set()
        )


class PowerWindowPolicyTests(unittest.TestCase):
    def test_power_window_must_keep_forty_db(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = make_settings(Path(folder), sample_state(0.2, 0.1))
            engine = PolicyEngine(settings)
            engine.raw.setdefault("analysis", {}).setdefault("02c", {})[
                "power_window_db"
            ] = 40
            base = {
                "qubits": ["q1"],
                "frequency_span_in_mhz": 2.0,
                "frequency_step_in_mhz": 0.03,
            }
            engine.validate_run("02c", {**base, "min_power_dbm": -42, "max_power_dbm": -2})
            with self.assertRaisesRegex(PolicyError, "exactly 40 dB"):
                engine.validate_run(
                    "02c", {**base, "min_power_dbm": -42, "max_power_dbm": -10}
                )


if __name__ == "__main__":
    unittest.main()
