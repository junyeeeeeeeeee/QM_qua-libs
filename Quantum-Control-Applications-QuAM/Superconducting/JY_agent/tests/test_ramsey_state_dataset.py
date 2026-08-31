from __future__ import annotations

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import xarray as xr


fetch_module = importlib.import_module(
    "quam_libs.experiments.ramsey.analysis.fetch_dataset"
)


class RamseyStateDatasetTests(unittest.TestCase):
    def test_state_only_dataset_skips_iq_voltage_conversion(self) -> None:
        idle_times = np.asarray([4, 8, 12])
        dataset = xr.Dataset(
            {
                "state": (
                    ("qubit", "time", "sign"),
                    np.zeros((1, 3, 2), dtype=float),
                )
            },
            coords={"qubit": ["q1"], "time": idle_times, "sign": [-1, 1]},
        )
        job = SimpleNamespace(result_handles=object())
        with (
            patch.object(
                fetch_module,
                "get_idle_times_in_clock_cycles",
                return_value=idle_times,
            ),
            patch.object(
                fetch_module,
                "fetch_results_as_xarray",
                return_value=dataset,
            ),
            patch.object(fetch_module, "convert_IQ_to_V") as convert,
        ):
            result = fetch_module.fetch_dataset(job, [SimpleNamespace(name="q1")], object())

        convert.assert_not_called()
        self.assertIn("state", result.data_vars)
        np.testing.assert_array_equal(result.time.values, 4 * idle_times)
        self.assertEqual(result.time.attrs["units"], "ns")


if __name__ == "__main__":
    unittest.main()
