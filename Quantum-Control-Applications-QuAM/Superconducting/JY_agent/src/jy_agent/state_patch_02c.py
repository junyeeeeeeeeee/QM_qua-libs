from __future__ import annotations

import math
from typing import Any

from .state import load_state, pointer_get


def derive_02c_state_patch(
    settings: Any,
    current: dict[str, Any],
    dataset_metrics: dict[str, Any],
    targets: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build 02c updates exclusively from JY's dressed-plateau analysis."""
    wiring = load_state(settings.wiring_path) if settings.wiring_path.is_file() else {}
    patch: list[dict[str, Any]] = []
    errors: list[str] = []
    metrics_by_qubit = dataset_metrics.get("qubits", {})
    for name in targets:
        metrics = metrics_by_qubit.get(name, {})
        transition = (
            metrics.get("02c_transition") if isinstance(metrics, dict) else None
        )
        if not isinstance(transition, dict):
            continue
        if transition.get("skip_subsequent_experiments") is True:
            continue
        if transition.get("validation_failures"):
            continue
        dressed_frequency = transition.get("dressed_frequency_hz")
        base_frequency = transition.get("base_frequency_hz")
        dressed_power = transition.get("dressed_power_limit_dbm")
        if not all(_finite(value) for value in (dressed_frequency, base_frequency, dressed_power)):
            errors.append(f"{name} JY dressed point is incomplete")
            continue
        try:
            qubit = current["qubits"][name]
            old_if = qubit["resonator"]["intermediate_frequency"]
            full_scale = _readout_full_scale_power_dbm(
                current, wiring, qubit
            )
        except Exception as exc:
            errors.append(f"{name} readout power context is unavailable: {exc}")
            continue
        if not _finite(old_if) or not _finite(full_scale):
            errors.append(f"{name} readout IF/full-scale power is not finite")
            continue
        new_if = float(old_if) + float(dressed_frequency) - float(base_frequency)
        amplitude = 10.0 ** ((float(dressed_power) - float(full_scale)) / 20.0)
        transition["jy_selected_frequency_hz"] = float(dressed_frequency)
        transition["jy_selected_power_dbm"] = float(dressed_power)
        transition["jy_readout_full_scale_power_dbm"] = float(full_scale)
        transition["jy_readout_amplitude"] = amplitude
        values = {
            f"/qubits/{name}/resonator/intermediate_frequency": new_if,
            f"/qubits/{name}/resonator/operations/readout/amplitude": amplitude,
            f"/qubits/{name}/extras/dressed_resonator_freq": float(dressed_frequency),
        }
        for path, value in values.items():
            try:
                pointer_get(current, path)
            except Exception:
                operation = "add"
            else:
                operation = "replace"
            patch.append({"op": operation, "path": path, "value": value})
    return patch, errors


def _readout_full_scale_power_dbm(
    state: dict[str, Any], wiring: dict[str, Any], qubit: dict[str, Any]
) -> float:
    readout = qubit["resonator"]["operations"]["readout"]
    if isinstance(readout, str) and readout.startswith("#./"):
        readout = qubit["resonator"]["operations"][readout[3:]]
    if isinstance(readout, dict) and _finite(readout.get("full_scale_power_dbm")):
        return float(readout["full_scale_power_dbm"])

    output: Any = qubit["resonator"].get("opx_output")
    if isinstance(output, str) and output.startswith("#/wiring/"):
        output = pointer_get(wiring, output[1:])
    if isinstance(output, str) and output.startswith("#/ports/"):
        output = pointer_get(state, output[1:])
    if not isinstance(output, dict) or not _finite(output.get("full_scale_power_dbm")):
        raise ValueError("cannot resolve resonator OPX full-scale power")
    return float(output["full_scale_power_dbm"])


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
