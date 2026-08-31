from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from jy_agent.analysis import _resolve_snapshot_npz_references


class SnapshotNpzReferenceTests(unittest.TestCase):
    def test_resolves_nested_snapshot_npz_scalars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            np.savez(
                snapshot / "arrays.npz",
                **{
                    "fit_results.q3.decay": np.asarray(4.25e-6),
                    "fit_results.q3.decay_error": np.asarray(0.25e-6),
                },
            )
            payload = {
                "fit_results": {
                    "q3": {
                        "decay": "./arrays.npz#fit_results.q3.decay",
                        "decay_error": "./arrays.npz#fit_results.q3.decay_error",
                        "raw_fit_results": "./fit_results.q3.raw_fit_results.h5",
                    }
                }
            }

            resolved = _resolve_snapshot_npz_references(snapshot, payload)

            self.assertEqual(resolved["fit_results"]["q3"]["decay"], 4.25e-6)
            self.assertEqual(
                resolved["fit_results"]["q3"]["decay_error"], 0.25e-6
            )
            self.assertEqual(
                resolved["fit_results"]["q3"]["raw_fit_results"],
                "./fit_results.q3.raw_fit_results.h5",
            )
            json.dumps(resolved)


if __name__ == "__main__":
    unittest.main()
