from __future__ import annotations

import math
from typing import Any

from .analysis_02c_absence import assess_bare_only_absence


DEFAULT_02C_RULES: dict[str, float | int] = {
    "plateau_tail_fraction": 0.20,
    "min_plateau_points": 4,
    "plateau_min_coverage": 0.70,
    "min_frequency_separation_steps": 4.0,
    "plateau_band_steps": 2.0,
    "plateau_band_fraction_of_separation": 0.15,
    "max_plateau_width_steps": 4.0,
    "max_plateau_width_fraction_of_separation": 0.35,
    "min_depletion_points": 2,
    "min_transition_monotonic_fraction": 0.70,
    "transition_monotonic_slack_fraction": 0.10,
    "frequency_edge_fraction": 0.05,
    "selected_power_tolerance_steps": 2.0,
    "smoothing_window_points": 3,
    "bare_only_max_trace_width_steps": 2.0,
    "bare_only_bare_match_steps": 3.0,
    "bare_only_required_min_power_dbm": -50.0,
    "bare_only_max_frequency_span_mhz": 10.0,
    "bare_only_max_frequency_step_mhz": 0.1,
    "bare_only_min_num_averages": 200,
    "bare_only_min_readout_length_ns": 1000,
    "bare_only_min_low_power_snr": 5.0,
    "bare_only_min_power_points": 40,
}


def analyze_02c_transitions(
    dataset: Any,
    configured_rules: dict[str, Any] | None = None,
    run_parameters: dict[str, Any] | None = None,
    qubit_context: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate dressed/depletion/bare behavior in a 02c power sweep.

    Power is sorted from low power (more-negative dBm) to high power.  The
    low-power tail must form the dressed-frequency plateau, the high-power
    tail must form a distinct bare-frequency plateau, and the points between
    them must move predominantly from dressed toward bare as power increases.
    """
    import numpy as np

    rules = dict(DEFAULT_02C_RULES)
    if configured_rules:
        rules.update(configured_rules)

    required_coordinates = {"power_dbm", "freq"}
    missing = sorted(required_coordinates - set(dataset.coords))
    if missing:
        return {
            "error": f"02c dataset is missing coordinates: {', '.join(missing)}.",
            "qubits": {},
            "rules": rules,
        }

    if "rr_min_response" in dataset.data_vars:
        trace = dataset["rr_min_response"]
        trace_source = "rr_min_response"
    else:
        source_name = next(
            (
                name
                for name in ("IQ_abs_norm", "IQ_abs")
                if name in dataset.data_vars
            ),
            None,
        )
        if source_name is None:
            return {
                "error": "02c dataset has no rr_min_response or IQ amplitude data.",
                "qubits": {},
                "rules": rules,
            }
        trace = dataset[source_name].idxmin(dim="freq")
        trace_source = f"{source_name}.idxmin(freq)"

    qubit_names = (
        [str(value) for value in dataset.coords["qubit"].values]
        if "qubit" in dataset.coords
        else ["unknown"]
    )
    frequency_axis = np.asarray(dataset.coords["freq"].values, dtype=float)
    power_axis = np.asarray(dataset.coords["power_dbm"].values, dtype=float)
    results: dict[str, Any] = {}
    for name in qubit_names:
        selected = trace.sel(qubit=name) if "qubit" in trace.dims else trace
        values = np.asarray(selected.values, dtype=float).reshape(-1)
        base_frequency = _base_frequency(dataset, name)
        transition = _analyze_trace(
            power_axis,
            values,
            frequency_axis,
            base_frequency,
            rules,
            trace_source,
        )
        absence = assess_bare_only_absence(
            dataset,
            name,
            transition,
            run_parameters,
            (qubit_context or {}).get(name, {}),
            rules,
        )
        transition.update(absence)
        if transition.get("skip_subsequent_experiments") is True:
            transition["validation_failures"] = []
        results[name] = transition
    return {"qubits": results, "rules": rules}


def selected_readout_power_dbm(result: Any) -> float | None:
    """Read an explicitly recorded physical power from a 02c fit result.

    Qualibrate's interactive state-update recorder suppresses assignments while
    capturing them. Consequently, ``full_scale_power_dbm`` and ``amplitude``
    in a node result can describe different states and must not be combined to
    reconstruct the sweep power. The saved derivative trace is authoritative.
    """
    if not isinstance(result, dict):
        return None
    for key in ("optimal_power_dbm", "readout_power_dbm", "power_dbm"):
        value = result.get(key)
        if _finite(value):
            return float(value)
    return None


def node_selected_readout_powers_dbm(
    dataset: Any, derivative_threshold_hz_per_dbm: Any
) -> dict[str, float | None]:
    """Reproduce the protected node's first derivative-threshold crossing.

    The selected power is the first power-grid coordinate whose saved smoothed
    derivative is below the configured threshold. This is also the y-position
    of the red horizontal line in the node's result figure.
    """
    import numpy as np

    if (
        not _finite(derivative_threshold_hz_per_dbm)
        or "power_dbm" not in dataset.coords
        or "rr_min_response_diff_avg" not in dataset.data_vars
    ):
        return {}
    threshold = float(derivative_threshold_hz_per_dbm)
    power = np.asarray(dataset.coords["power_dbm"].values, dtype=float).reshape(-1)
    derivative = dataset["rr_min_response_diff_avg"]
    qubit_names = (
        [str(value) for value in dataset.coords["qubit"].values]
        if "qubit" in dataset.coords
        else ["unknown"]
    )
    selected: dict[str, float | None] = {}
    for name in qubit_names:
        values = derivative.sel(qubit=name) if "qubit" in derivative.dims else derivative
        values = np.asarray(values.values, dtype=float).reshape(-1)
        if values.size != power.size:
            selected[name] = None
            continue
        crossings = np.flatnonzero(
            np.isfinite(power) & np.isfinite(values) & (values < threshold)
        )
        selected[name] = float(power[crossings[0]]) if crossings.size else None
    return selected


def _analyze_trace(
    powers: Any,
    frequencies: Any,
    frequency_axis: Any,
    base_frequency: float | None,
    rules: dict[str, Any],
    trace_source: str,
) -> dict[str, Any]:
    import numpy as np

    power = np.asarray(powers, dtype=float).reshape(-1)
    tracked = np.asarray(frequencies, dtype=float).reshape(-1)
    finite = np.isfinite(power) & np.isfinite(tracked)
    power = power[finite]
    tracked = tracked[finite]
    order = np.argsort(power)
    power = power[order]
    tracked = tracked[order]
    failures: list[str] = []

    min_plateau_points = max(int(rules["min_plateau_points"]), 2)
    min_depletion_points = max(int(rules["min_depletion_points"]), 1)
    minimum_samples = 2 * min_plateau_points + min_depletion_points
    if power.size < minimum_samples:
        return {
            "error": f"at least {minimum_samples} finite power samples are required",
            "validation_failures": [
                f"only {power.size} finite power samples are available"
            ],
            "trace_source": trace_source,
        }

    unique_frequency = np.sort(
        np.unique(np.asarray(frequency_axis, dtype=float)[np.isfinite(frequency_axis)])
    )
    frequency_steps = np.diff(unique_frequency)
    frequency_steps = frequency_steps[frequency_steps > 0]
    if unique_frequency.size < 2 or frequency_steps.size == 0:
        return {
            "error": "frequency sweep coordinates do not increase",
            "validation_failures": ["frequency sweep coordinates do not increase"],
            "trace_source": trace_source,
        }
    frequency_step = float(np.median(frequency_steps))
    sweep_min = float(unique_frequency[0])
    sweep_max = float(unique_frequency[-1])
    sweep_span = sweep_max - sweep_min

    power_steps = np.diff(power)
    power_steps = power_steps[power_steps > 0]
    if power_steps.size == 0:
        return {
            "error": "power sweep coordinates do not increase",
            "validation_failures": ["power sweep coordinates do not increase"],
            "trace_source": trace_source,
        }
    power_step = float(np.median(power_steps))

    window = max(int(rules["smoothing_window_points"]), 1)
    if window % 2 == 0:
        window += 1
    smoothed = _rolling_median(tracked, window)
    tail_count = max(
        min_plateau_points,
        int(math.ceil(power.size * float(rules["plateau_tail_fraction"]))),
    )
    tail_count = min(tail_count, (power.size - min_depletion_points) // 2)

    dressed_frequency = float(np.median(smoothed[:tail_count]))
    bare_frequency = float(np.median(smoothed[-tail_count:]))
    signed_separation = bare_frequency - dressed_frequency
    separation = abs(signed_separation)
    minimum_separation = float(rules["min_frequency_separation_steps"]) * frequency_step
    if separation < minimum_separation:
        failures.append(
            "bare and dressed plateaus are not distinct by the required frequency separation"
        )

    band = max(
        float(rules["plateau_band_steps"]) * frequency_step,
        float(rules["plateau_band_fraction_of_separation"]) * separation,
    )
    dressed_tail = smoothed[:tail_count]
    bare_tail = smoothed[-tail_count:]
    dressed_coverage = float(np.mean(np.abs(dressed_tail - dressed_frequency) <= band))
    bare_coverage = float(np.mean(np.abs(bare_tail - bare_frequency) <= band))
    minimum_coverage = float(rules["plateau_min_coverage"])
    if dressed_coverage < minimum_coverage:
        failures.append("low-power dressed plateau is not stable")
    if bare_coverage < minimum_coverage:
        failures.append("high-power bare plateau is not stable")

    dressed_width = _central_width(dressed_tail)
    bare_width = _central_width(bare_tail)
    maximum_width = max(
        float(rules["max_plateau_width_steps"]) * frequency_step,
        float(rules["max_plateau_width_fraction_of_separation"]) * separation,
    )
    if dressed_width > maximum_width:
        failures.append("low-power dressed plateau width is too large")
    if bare_width > maximum_width:
        failures.append("high-power bare plateau width is too large")

    dressed_edge_fraction = _edge_fraction(
        dressed_frequency, sweep_min, sweep_max
    )
    bare_edge_fraction = _edge_fraction(bare_frequency, sweep_min, sweep_max)
    minimum_edge = float(rules["frequency_edge_fraction"])
    if dressed_edge_fraction < minimum_edge:
        failures.append("dressed plateau lies within 5% of a frequency-sweep edge")
    if bare_edge_fraction < minimum_edge:
        failures.append("bare plateau lies within 5% of a frequency-sweep edge")

    dressed_end: int | None = None
    bare_start: int | None = None
    depletion_points = 0
    monotonic_fraction = 0.0
    if separation >= minimum_separation:
        normalized = (smoothed - dressed_frequency) / signed_separation
        band_fraction = min(band / separation, 0.45)
        dressed_mask = np.abs(normalized) <= band_fraction
        bare_mask = np.abs(normalized - 1.0) <= band_fraction
        confirmation_points = 2
        transition_start = _first_confirmed_false(
            dressed_mask, tail_count, confirmation_points
        )
        if transition_start is None:
            failures.append("no exit from the low-power dressed plateau was found")
        else:
            dressed_indices = np.flatnonzero(dressed_mask[:transition_start])
            if dressed_indices.size:
                dressed_end = int(dressed_indices[-1])
            else:
                failures.append("no low-power dressed plateau points were found")

        if transition_start is not None:
            bare_start = _bare_suffix_start(
                bare_mask,
                max(transition_start + 1, tail_count),
                min_plateau_points,
                minimum_coverage,
            )
            if bare_start is None:
                failures.append("no stable high-power bare plateau was found")

        if dressed_end is not None and bare_start is not None:
            transition = normalized[dressed_end : bare_start + 1]
            interior = transition[
                (transition > band_fraction)
                & (transition < 1.0 - band_fraction)
            ]
            depletion_points = int(interior.size)
            if depletion_points < min_depletion_points:
                failures.append("depletion region has too few intermediate-frequency points")
            differences = np.diff(transition)
            if differences.size:
                slack = float(rules["transition_monotonic_slack_fraction"])
                monotonic_fraction = float(np.mean(differences >= -slack))
            if monotonic_fraction < float(rules["min_transition_monotonic_fraction"]):
                failures.append(
                    "depletion-region frequency does not move consistently from dressed to bare as power increases"
                )

    dressed_limit = (
        float(power[dressed_end]) if dressed_end is not None else None
    )
    selected_tolerance = (
        float(rules["selected_power_tolerance_steps"]) * power_step
    )
    absolute_dressed = (
        float(base_frequency + dressed_frequency)
        if base_frequency is not None
        else None
    )
    absolute_bare = (
        float(base_frequency + bare_frequency)
        if base_frequency is not None
        else None
    )
    return {
        "trace_source": trace_source,
        "base_frequency_hz": base_frequency,
        "samples": int(power.size),
        "low_power_dbm": float(power[0]),
        "high_power_dbm": float(power[-1]),
        "power_step_db": power_step,
        "dressed_frequency_offset_hz": dressed_frequency,
        "bare_frequency_offset_hz": bare_frequency,
        "dressed_frequency_hz": absolute_dressed,
        "bare_frequency_hz": absolute_bare,
        "bare_dressed_separation_hz": separation,
        "dressed_plateau_coverage": dressed_coverage,
        "bare_plateau_coverage": bare_coverage,
        "dressed_plateau_width_hz": dressed_width,
        "bare_plateau_width_hz": bare_width,
        "dressed_frequency_edge_fraction": dressed_edge_fraction,
        "bare_frequency_edge_fraction": bare_edge_fraction,
        "depletion_point_count": depletion_points,
        "transition_monotonic_fraction": monotonic_fraction,
        "dressed_power_limit_dbm": dressed_limit,
        "selected_power_tolerance_db": selected_tolerance,
        "validation_failures": failures,
    }


def _base_frequency(dataset: Any, qubit: str) -> float | None:
    import numpy as np

    if "freq_full" not in dataset.coords:
        return None
    full = dataset.coords["freq_full"]
    if "qubit" in full.dims:
        full = full.sel(qubit=qubit)
    frequency = np.asarray(dataset.coords["freq"].values, dtype=float)
    full_values = np.asarray(full.values, dtype=float)
    if full_values.shape != frequency.shape:
        return None
    difference = full_values - frequency
    finite = difference[np.isfinite(difference)]
    return float(np.median(finite)) if finite.size else None


def _rolling_median(values: Any, window: int) -> Any:
    import numpy as np

    source = np.asarray(values, dtype=float)
    half = window // 2
    return np.asarray(
        [
            np.median(source[max(0, index - half) : min(source.size, index + half + 1)])
            for index in range(source.size)
        ],
        dtype=float,
    )


def _central_width(values: Any) -> float:
    import numpy as np

    source = np.asarray(values, dtype=float)
    return float(np.percentile(source, 90) - np.percentile(source, 10))


def _edge_fraction(value: float, minimum: float, maximum: float) -> float:
    span = maximum - minimum
    if span <= 0:
        return 0.0
    return max(0.0, min(value - minimum, maximum - value) / span)


def _first_confirmed_false(mask: Any, start: int, count: int) -> int | None:
    import numpy as np

    values = np.asarray(mask, dtype=bool)
    for index in range(start, values.size - count + 1):
        if not bool(np.any(values[index : index + count])):
            return index
    return None


def _bare_suffix_start(
    mask: Any, start: int, confirmation_points: int, minimum_coverage: float
) -> int | None:
    import numpy as np

    values = np.asarray(mask, dtype=bool)
    for index in range(start, values.size - confirmation_points + 1):
        if (
            bool(np.all(values[index : index + confirmation_points]))
            and float(np.mean(values[index:])) >= minimum_coverage
        ):
            return index
    return None


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
