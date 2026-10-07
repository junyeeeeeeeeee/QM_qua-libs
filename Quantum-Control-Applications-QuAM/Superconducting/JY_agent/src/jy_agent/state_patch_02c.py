from __future__ import annotations

import math
from typing import Any

from .state import load_state, pointer_get


def derive_02c_state_patch(
    settings: Any,
    current: dict[str, Any],
    dataset_metrics: dict[str, Any],
    targets: list[str],
    policy_raw: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build 02c updates exclusively from JY's dressed-plateau analysis.

    Operator instruction 2026-10-02: the readout power is the dressed boundary
    minus ``readout_power_backoff_db``, and each readout line's full-scale
    power is the smallest integer that keeps every pulse amplitude and the
    line's summed multiplex amplitude inside policy.
    """
    wiring = load_state(settings.wiring_path) if settings.wiring_path.is_file() else {}
    limits = _readout_limits(settings, policy_raw)
    patch: list[dict[str, Any]] = []
    errors: list[str] = []
    metrics_by_qubit = dataset_metrics.get("qubits", {})
    selected: dict[str, dict[str, Any]] = {}
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
            line = _readout_line(current, wiring, name)
        except Exception as exc:
            errors.append(f"{name} readout power context is unavailable: {exc}")
            continue
        if not _finite(old_if) or not _finite(line["full_scale"]):
            errors.append(f"{name} readout IF/full-scale power is not finite")
            continue
        selected[name] = {
            "transition": transition,
            "line": line,
            "power": float(dressed_power) - limits["backoff_db"],
            "if": float(old_if) + float(dressed_frequency) - float(base_frequency),
            "dressed_frequency": float(dressed_frequency),
        }

    lines: dict[str, list[str]] = {}
    for name, item in selected.items():
        lines.setdefault(item["line"]["key"], []).append(name)
    for key, names in sorted(lines.items()):
        line = selected[names[0]]["line"]
        powers = {name: selected[name]["power"] for name in names}
        full_scale = float(line["full_scale"])
        others = [name for name in line["members"] if name not in powers]
        if line["path"] is not None and not others:
            chosen = _smallest_full_scale(powers, limits)
            if chosen is not None:
                full_scale = float(chosen)
                if chosen != line["full_scale"]:
                    patch.append(
                        {"op": "replace", "path": line["path"], "value": int(chosen)}
                    )
        amplitudes = {
            name: 10.0 ** ((power - full_scale) / 20.0)
            for name, power in powers.items()
        }
        aggregate = sum(amplitudes.values()) + sum(
            _current_readout_amplitude(current, name) for name in others
        )
        line_errors: list[str] = []
        if aggregate > limits["max_aggregate"]:
            line_errors.append(
                f"summed readout amplitude {aggregate:.4g} on {key} exceeds "
                f"{limits['max_aggregate']:g} at full scale {full_scale:g} dBm"
            )
        for name in names:
            amplitude = amplitudes[name]
            reasons = list(line_errors)
            if amplitude > limits["max_pulse"]:
                reasons.append(
                    f"readout amplitude {amplitude:.4g} exceeds "
                    f"{limits['max_pulse']:g} at full scale {full_scale:g} dBm"
                )
            if reasons:
                errors.extend(f"{name} {reason}" for reason in reasons)
                continue
            item = selected[name]
            transition = item["transition"]
            transition["jy_selected_frequency_hz"] = item["dressed_frequency"]
            transition["jy_readout_power_backoff_db"] = limits["backoff_db"]
            transition["jy_selected_power_dbm"] = item["power"]
            transition["jy_readout_full_scale_power_dbm"] = full_scale
            transition["jy_readout_amplitude"] = amplitude
            values = {
                f"/qubits/{name}/resonator/intermediate_frequency": item["if"],
                f"/qubits/{name}/resonator/operations/readout/amplitude": amplitude,
                f"/qubits/{name}/extras/dressed_resonator_freq": item["dressed_frequency"],
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


def reconcile_02c_full_scale(
    settings: Any,
    current: dict[str, Any],
    kept: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
    passing: set[str],
    policy_raw: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep a readout line's full scale and amplitudes consistent after a split.

    A ``needs_review`` run commits only per-qubit entries, so a shared port's
    full-scale change is dropped. It is restored when every qubit on that line
    passed. Otherwise the line keeps its current full scale and the passing
    qubits' amplitudes are rescaled so they still deliver the selected power.
    """
    wiring = load_state(settings.wiring_path) if settings.wiring_path.is_file() else {}
    limits = _readout_limits(settings, policy_raw)
    warnings: list[str] = []
    result = list(kept)
    for item in dropped:
        path = str(item.get("path", ""))
        if not (path.startswith("/ports/") and path.endswith("/full_scale_power_dbm")):
            continue
        members = _port_members(current, wiring, path)
        if members and set(members) <= passing:
            result.append(item)
            continue
        old_full_scale = float(pointer_get(current, path))
        scale = 10.0 ** ((float(item["value"]) - old_full_scale) / 20.0)
        rescaled: list[dict[str, Any]] = []
        for entry in result:
            name = _amplitude_owner(entry, members)
            if name is None:
                rescaled.append(entry)
                continue
            amplitude = float(entry["value"]) * scale
            if amplitude > limits["max_pulse"]:
                warnings.append(
                    f"02c readout amplitude for {name} is suppressed: at the "
                    f"current full scale {old_full_scale:g} dBm it would be "
                    f"{amplitude:.4g} > {limits['max_pulse']:g}."
                )
                continue
            rescaled.append({**entry, "value": amplitude})
        result = rescaled
        warnings.append(
            f"02c keeps {path} at {old_full_scale:g} dBm because not every qubit "
            "on that readout line passed; passing amplitudes were rescaled."
        )
    return result, warnings


def _smallest_full_scale(
    powers: dict[str, float], limits: dict[str, float]
) -> int | None:
    for full_scale in range(int(limits["full_scale_min"]), int(limits["full_scale_max"]) + 1):
        amplitudes = [10.0 ** ((power - full_scale) / 20.0) for power in powers.values()]
        if max(amplitudes) <= limits["max_pulse"] and sum(amplitudes) <= limits["max_aggregate"]:
            return full_scale
    return None


def _readout_limits(settings: Any, policy_raw: dict[str, Any] | None) -> dict[str, float]:
    if policy_raw is None:
        from .policy import PolicyEngine

        policy_raw = PolicyEngine(settings).raw
    rules = policy_raw.get("analysis", {}).get("02c", {})
    instrument = policy_raw.get("instrument_limits", {})
    full_scale = instrument.get("opx1000_full_scale_power_dbm", {})
    return {
        "backoff_db": float(rules.get("readout_power_backoff_db", 0.0)),
        "max_pulse": float(
            rules.get(
                "max_readout_pulse_amplitude",
                instrument.get("mw", {}).get("max_wf_amplitude", 1.0),
            )
        ),
        "max_aggregate": float(
            instrument.get("multiplex", {}).get("max_aggregate_readout_amplitude", 1.0)
        ),
        "full_scale_min": float(full_scale.get("min", -11)),
        "full_scale_max": float(full_scale.get("max", 16)),
    }


def _readout_line(
    state: dict[str, Any], wiring: dict[str, Any], name: str
) -> dict[str, Any]:
    """Describe where a qubit's readout full-scale power lives.

    A full scale stored on the readout operation belongs to that qubit alone
    and is left unchanged. A port-level full scale is shared by every qubit
    wired to that port.
    """
    qubit = state["qubits"][name]
    readout = _readout_operation(qubit)
    if isinstance(readout, dict) and _finite(readout.get("full_scale_power_dbm")):
        return {
            "key": f"/qubits/{name}/resonator/operations/readout",
            "path": None,
            "full_scale": float(readout["full_scale_power_dbm"]),
            "members": [name],
        }
    reference = _output_reference(state, wiring, name)
    if reference is None:
        raise ValueError("cannot resolve resonator OPX full-scale power")
    port = pointer_get(state, reference)
    if not isinstance(port, dict) or not _finite(port.get("full_scale_power_dbm")):
        raise ValueError("cannot resolve resonator OPX full-scale power")
    return {
        "key": reference,
        "path": f"{reference}/full_scale_power_dbm",
        "full_scale": port["full_scale_power_dbm"],
        "members": _port_members(state, wiring, f"{reference}/full_scale_power_dbm"),
    }


def _output_reference(
    state: dict[str, Any], wiring: dict[str, Any], name: str
) -> str | None:
    output: Any = state["qubits"][name].get("resonator", {}).get("opx_output")
    if isinstance(output, str) and output.startswith("#/wiring/"):
        output = pointer_get(wiring, output[1:])
    if output is None:
        output = (
            wiring.get("wiring", wiring)
            .get("qubits", {})
            .get(name, {})
            .get("rr", {})
            .get("opx_output")
        )
    if isinstance(output, str) and output.startswith("#/ports/"):
        return output[1:]
    return None


def readout_port_members(
    state: dict[str, Any], wiring: dict[str, Any], full_scale_path: str
) -> list[str]:
    """Qubits whose readout is wired to the port owning ``full_scale_path``."""
    return _port_members(state, wiring, full_scale_path)


def _port_members(
    state: dict[str, Any], wiring: dict[str, Any], full_scale_path: str
) -> list[str]:
    reference = full_scale_path.rsplit("/", 1)[0]
    members: list[str] = []
    for name in state.get("qubits", {}):
        try:
            if _output_reference(state, wiring, name) == reference:
                members.append(name)
        except Exception:
            continue
    return sorted(members)


def _amplitude_owner(entry: dict[str, Any], members: list[str]) -> str | None:
    for name in members:
        if entry.get("path") == f"/qubits/{name}/resonator/operations/readout/amplitude":
            return name
    return None


def _readout_operation(qubit: dict[str, Any]) -> Any:
    operations = qubit["resonator"]["operations"]
    readout = operations["readout"]
    if isinstance(readout, str) and readout.startswith("#./"):
        readout = operations[readout[3:]]
    return readout


def _current_readout_amplitude(state: dict[str, Any], name: str) -> float:
    try:
        amplitude = _readout_operation(state["qubits"][name]).get("amplitude")
    except Exception:
        return 0.0
    return abs(float(amplitude)) if _finite(amplitude) else 0.0


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
