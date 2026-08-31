from __future__ import annotations

import math
from typing import Any


def assess_bare_only_absence(
    dataset: Any,
    qubit: str,
    transition: dict[str, Any],
    run_parameters: dict[str, Any] | None,
    qubit_context: dict[str, Any] | None,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Return strict evidence for treating a flat bare-only trace as absent.

    A missing dressed transition is accepted only when the tracked resonance is
    a resolved, centered, straight bare-frequency line and the low-power,
    frequency-resolution, and acquisition-depth safeguards all pass.
    """
    import numpy as np

    parameters = run_parameters or {}
    context = qubit_context or {}
    frequency = np.asarray(dataset.coords["freq"].values, dtype=float)
    power = np.asarray(dataset.coords["power_dbm"].values, dtype=float)
    finite_frequency = np.sort(np.unique(frequency[np.isfinite(frequency)]))
    finite_power = power[np.isfinite(power)]
    steps = np.diff(finite_frequency)
    steps = steps[steps > 0]
    if finite_frequency.size < 2 or finite_power.size == 0 or steps.size == 0:
        return {
            "classification": "ambiguous",
            "skip_subsequent_experiments": False,
            "bare_only_checks": {"valid_axes": False},
        }

    frequency_step = float(np.median(steps))
    minimum_separation = (
        float(rules["min_frequency_separation_steps"]) * frequency_step
    )
    separation = transition.get("bare_dressed_separation_hz")
    if _finite(separation) and float(separation) >= minimum_separation:
        return {
            "classification": "dressed_transition",
            "skip_subsequent_experiments": False,
            "bare_only_checks": {"transition_is_not_distinct": False},
        }

    trace = dataset.get("rr_min_response")
    if trace is None:
        return {
            "classification": "ambiguous",
            "skip_subsequent_experiments": False,
            "bare_only_checks": {"rr_min_response_present": False},
        }
    if "qubit" in trace.dims:
        trace = trace.sel(qubit=qubit)
    tracked = np.asarray(trace.values, dtype=float).reshape(-1)
    tracked = tracked[np.isfinite(tracked)]
    if tracked.size == 0:
        return {
            "classification": "ambiguous",
            "skip_subsequent_experiments": False,
            "bare_only_checks": {"finite_trace_present": False},
        }

    center_offset = float(np.median(tracked))
    trace_width = float(np.percentile(tracked, 95) - np.percentile(tracked, 5))
    base_frequency = transition.get("base_frequency_hz")
    center_frequency = (
        float(base_frequency) + center_offset if _finite(base_frequency) else None
    )
    expected_bare = context.get("expected_bare_frequency_hz")
    bare_error = (
        abs(float(center_frequency) - float(expected_bare))
        if _finite(center_frequency) and _finite(expected_bare)
        else None
    )
    configured_span = parameters.get("frequency_span_in_mhz")
    configured_step = parameters.get("frequency_step_in_mhz")
    averages = parameters.get("num_averages")
    readout_length = context.get("readout_length_ns")
    low_power_snr = _low_power_resonance_snr(dataset, qubit, rules)
    edge_fraction = _edge_fraction(
        center_offset,
        float(finite_frequency[0]),
        float(finite_frequency[-1]),
    )

    acquisition_sufficient = (
        _finite(readout_length)
        and float(readout_length) >= float(rules["bare_only_min_readout_length_ns"])
    ) or (
        _finite(averages)
        and float(averages) >= float(rules["bare_only_min_num_averages"])
    )
    checks = {
        "transition_is_not_distinct": True,
        "straight_bare_trace": trace_width
        <= float(rules["bare_only_max_trace_width_steps"]) * frequency_step,
        "matches_expected_bare_frequency": bare_error is not None
        and bare_error
        <= float(rules["bare_only_bare_match_steps"]) * frequency_step,
        "low_power_reached": float(np.min(finite_power))
        <= float(rules["bare_only_required_min_power_dbm"]),
        "frequency_span_is_narrow": _finite(configured_span)
        and float(configured_span)
        <= float(rules["bare_only_max_frequency_span_mhz"]),
        "frequency_step_is_fine": _finite(configured_step)
        and float(configured_step)
        <= float(rules["bare_only_max_frequency_step_mhz"]),
        "enough_power_points": finite_power.size
        >= int(rules["bare_only_min_power_points"]),
        "acquisition_is_sufficient": acquisition_sufficient,
        "resonance_is_resolved_at_low_power": _finite(low_power_snr)
        and float(low_power_snr) >= float(rules["bare_only_min_low_power_snr"]),
        "bare_line_is_not_edge_clipped": edge_fraction
        >= float(rules["frequency_edge_fraction"]),
    }
    absent = bool(all(checks.values()))
    return {
        "classification": "bare_only_absent" if absent else "ambiguous",
        "skip_subsequent_experiments": absent,
        "bare_only_checks": checks,
        "bare_only_evidence": {
            "trace_center_frequency_hz": center_frequency,
            "expected_bare_frequency_hz": expected_bare,
            "bare_frequency_error_hz": bare_error,
            "trace_width_hz": trace_width,
            "low_power_snr": low_power_snr,
            "readout_length_ns": readout_length,
            "num_averages": averages,
            "configured_frequency_span_mhz": configured_span,
            "configured_frequency_step_mhz": configured_step,
            "minimum_power_dbm": float(np.min(finite_power)),
        },
    }


def _low_power_resonance_snr(
    dataset: Any, qubit: str, rules: dict[str, Any]
) -> float | None:
    import numpy as np

    source_name = next(
        (name for name in ("IQ_abs_norm", "IQ_abs") if name in dataset.data_vars),
        None,
    )
    if source_name is None:
        return None
    signal = dataset[source_name]
    if "qubit" in signal.dims:
        signal = signal.sel(qubit=qubit)
    if "power_dbm" not in signal.dims or "freq" not in signal.dims:
        return None
    signal = signal.transpose("power_dbm", "freq", ...)
    values = np.asarray(signal.values, dtype=float)
    while values.ndim > 2:
        values = np.nanmean(values, axis=-1)
    tail_count = max(
        int(rules["min_plateau_points"]),
        int(math.ceil(values.shape[0] * float(rules["plateau_tail_fraction"]))),
    )
    scores: list[float] = []
    for row in values[:tail_count]:
        row = row[np.isfinite(row)]
        if row.size < 5:
            continue
        differences = np.diff(row)
        noise = float(
            np.median(np.abs(differences - np.median(differences))) * 1.4826
        )
        dynamic_range = float(np.max(row) - np.min(row))
        scores.append(dynamic_range / max(noise, 1e-15))
    return float(np.median(scores)) if scores else None


def _edge_fraction(value: float, minimum: float, maximum: float) -> float:
    span = maximum - minimum
    if span <= 0:
        return 0.0
    return max(0.0, min(value - minimum, maximum - value) / span)


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
