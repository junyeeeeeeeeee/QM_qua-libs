from __future__ import annotations

import math
from typing import Any, Iterable

from .util import json_loads


def resolve_02c_targets(
    run_rows: Iterable[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Return resolved and confirmed-absent targets from passing 02c analyses."""
    resolved: set[str] = set()
    absent: set[str] = set()
    for row in run_rows:
        if row.get("analysis_status") != "pass":
            continue
        analysis = json_loads(row.get("analysis_json"), {})
        qubits = analysis.get("dataset_metrics", {}).get("qubits", {})
        for name, metrics in qubits.items():
            transition = (
                metrics.get("02c_transition")
                if isinstance(metrics, dict)
                else None
            )
            if not isinstance(transition, dict):
                continue
            if transition.get("skip_subsequent_experiments") is True:
                resolved.add(str(name))
                absent.add(str(name))
                continue
            if transition.get("validation_failures"):
                continue
            frequency = transition.get("dressed_frequency_hz")
            power = transition.get("dressed_power_limit_dbm")
            if _finite(frequency) and _finite(power):
                resolved.add(str(name))
    return resolved, absent


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
