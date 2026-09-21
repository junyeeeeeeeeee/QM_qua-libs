from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .config import Settings, load_policies
from .state import (
    apply_json_patch,
    channel_kind,
    load_state,
    operation_amplitude,
    operation_backing_name,
    pointer_get,
)
from .util import is_finite_number


class PolicyError(ValueError):
    pass


class PolicyEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.raw = load_policies(settings)
        self.nodes: dict[str, dict[str, Any]] = self.raw["nodes"]
        self.limits: dict[str, Any] = self.raw["instrument_limits"]

    def node_definition(self, node_id: str) -> dict[str, Any]:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise PolicyError(
                f"Node {node_id!r} is not allowlisted. Allowed: {sorted(self.nodes)}"
            ) from exc

    def node_script(self, node_id: str) -> Path:
        definition = self.node_definition(node_id)
        script_root = str(definition.get("script_root", "calibration_graph"))
        roots = {
            "calibration_graph": self.settings.calibration_graph.resolve(),
            "side_project": (self.settings.superconducting_root / "side_project").resolve(),
        }
        if script_root not in roots:
            raise PolicyError(f"Unsupported script_root for {node_id}: {script_root}")
        root = roots[script_root]
        script = (root / definition["script"]).resolve()
        try:
            script.relative_to(root)
        except ValueError as exc:
            raise PolicyError(f"Allowlisted script for {node_id} escapes its root") from exc
        if not script.is_file():
            raise PolicyError(f"Invalid or missing allowlisted script for {node_id}")
        return script

    def validate_run(
        self, node_id: str, parameters: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(parameters, dict):
            raise PolicyError("parameters must be an object")
        definition = self.node_definition(node_id)
        rules: dict[str, str] = definition["allowed_parameters"]
        unknown = sorted(set(parameters) - set(rules))
        if unknown:
            raise PolicyError(f"Unsupported parameters for {node_id}: {unknown}")

        merged = {**definition.get("defaults", {}), **parameters}
        if self.settings.require_explicit_qubits and "qubits" not in parameters:
            raise PolicyError("An explicit qubits list is required for every run")
        for key, value in merged.items():
            if key == "qubits" or key in rules:
                self._validate_type(key, value, rules.get(key, "qubit_list"))

        state = load_state(self.settings.active_state)
        qubits = merged.get("qubits")
        if not isinstance(qubits, list) or not qubits:
            raise PolicyError("qubits must contain at least one explicit target")
        missing = [name for name in qubits if name not in state.get("qubits", {})]
        if missing:
            raise PolicyError(f"Unknown qubits in active state: {missing}")
        if len(set(qubits)) != len(qubits):
            raise PolicyError("qubits must not contain duplicates")

        self._validate_common_sweep(merged)
        self._validate_node_specific(node_id, merged, state)
        warnings: list[str] = []
        if definition["flux_behavior"] == "hardcoded_min":
            warnings.append(
                f"{node_id} applies the node's hardcoded minimum-flux behavior; "
                "the runner cannot override it."
            )
        else:
            warnings.append(f"{node_id} is constrained to joint flux mode.")
        if merged.get("multiplexed") is True and len(qubits) > 1:
            warnings.extend(self.validate_multiplex_targets(qubits, state))
        return merged, warnings

    def validate_multiplex_targets(
        self,
        qubits: list[str],
        state: dict[str, Any] | None = None,
    ) -> list[str]:
        """Validate shared-readout topology before a multi-qubit run."""
        limits = self.limits["multiplex"]
        if len(qubits) > int(limits["max_qubits_per_run"]):
            raise PolicyError(
                f"Multiplex run has {len(qubits)} qubits; policy maximum is "
                f"{limits['max_qubits_per_run']}"
            )
        active_state = state or load_state(self.settings.active_state)
        wiring = load_state(self.settings.wiring_path)
        wiring_qubits = wiring.get("wiring", wiring).get("qubits", {})
        groups: dict[str, dict[str, Any]] = {}
        for name in qubits:
            qubit = active_state.get("qubits", {}).get(name)
            topology = wiring_qubits.get(name, {}).get("rr", {})
            if not isinstance(qubit, dict):
                raise PolicyError(f"Multiplex target {name} is missing from state")
            output = topology.get("opx_output")
            input_ = topology.get("opx_input")
            if not isinstance(output, str) or not isinstance(input_, str):
                raise PolicyError(
                    f"{name} has no complete resonator OPX input/output wiring"
                )
            resonator = qubit.get("resonator", {})
            frequency = resonator.get("intermediate_frequency")
            if not is_finite_number(frequency):
                raise PolicyError(f"{name} resonator IF is not numeric")
            operations = resonator.get("operations", {})
            readout = operations.get("readout")
            if isinstance(readout, str) and readout.startswith("#./"):
                readout = operations.get(readout[3:])
            amplitude = readout.get("amplitude") if isinstance(readout, dict) else None
            if not is_finite_number(amplitude) or float(amplitude) <= 0:
                raise PolicyError(
                    f"{name} readout amplitude must be finite and positive"
                )
            group = groups.setdefault(
                output,
                {"input": input_, "qubits": []},
            )
            if group["input"] != input_:
                raise PolicyError(
                    f"Readout output {output} maps to multiple acquisition inputs"
                )
            group["qubits"].append(
                {
                    "name": name,
                    "frequency": float(frequency),
                    "amplitude": abs(float(amplitude)),
                }
            )

        descriptions: list[str] = []
        for output, group in sorted(groups.items()):
            members = group["qubits"]
            if len(members) > int(limits["max_qubits_per_readout_line"]):
                raise PolicyError(
                    f"Readout line {output} has {len(members)} targets; policy "
                    f"maximum is {limits['max_qubits_per_readout_line']}"
                )
            ordered = sorted(members, key=lambda item: item["frequency"])
            for left, right in zip(ordered, ordered[1:]):
                spacing = right["frequency"] - left["frequency"]
                if spacing < float(limits["min_resonator_spacing_hz"]):
                    raise PolicyError(
                        f"{left['name']} and {right['name']} are only "
                        f"{spacing / 1e6:g} MHz apart on {output}"
                    )
            aggregate = sum(item["amplitude"] for item in members)
            if aggregate > float(limits["max_aggregate_readout_amplitude"]):
                raise PolicyError(
                    f"Aggregate readout amplitude {aggregate:g} on {output} "
                    f"exceeds {limits['max_aggregate_readout_amplitude']:g}"
                )
            names = ",".join(item["name"] for item in members)
            descriptions.append(f"{names} on {output}")
        return [
            f"Multiplex topology validated for {len(qubits)} qubits across "
            f"{len(groups)} readout lines: {'; '.join(descriptions)}."
        ]

    def _validate_type(self, key: str, value: Any, rule: str) -> None:
        valid = True
        if rule == "qubit_list":
            valid = (
                isinstance(value, list)
                and all(isinstance(item, str) and re.fullmatch(r"q[0-9]+", item) for item in value)
            )
        elif rule == "positive_int":
            valid = isinstance(value, int) and not isinstance(value, bool) and value > 0
        elif rule == "optional_positive_int":
            valid = value is None or (
                isinstance(value, int) and not isinstance(value, bool) and value > 0
            )
        elif rule == "optional_int":
            valid = value is None or (
                isinstance(value, int) and not isinstance(value, bool)
            )
        elif rule == "int":
            # For a protected node that types the field as `int`. A fractional
            # value crashes its Parameters model before any hardware runs; see
            # the 2026-09-18 02c power-limit lesson.
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif rule == "bool":
            valid = isinstance(value, bool)
        elif rule == "number":
            valid = is_finite_number(value)
        elif rule == "optional_number":
            valid = value is None or is_finite_number(value)
        elif rule == "positive_number":
            valid = is_finite_number(value) and float(value) > 0
        elif rule == "nonnegative_number":
            valid = is_finite_number(value) and float(value) >= 0
        elif rule == "joint_literal":
            valid = value == "joint"
        elif rule == "saturation_literal":
            valid = value == "saturation"
        elif rule == "xy_operation_literal":
            valid = value in {"x180", "x90", "-x90", "y90", "-y90"}
        elif rule == "reset_literal":
            valid = value in {"thermal", "active"}
        elif rule == "rb_reset_literal":
            valid = value in {"thermal", "active", "active_gef"}
        elif rule == "readout_mode_literal":
            valid = value in {"ge", "gef"}
        elif rule == "linear_log_literal":
            valid = value in {"linear", "log"}
        elif rule == "log_lin_literal":
            valid = value in {"log", "lin"}
        elif rule == "plot_dimension_literal":
            valid = value in {"2D", "3D"}
        else:
            raise PolicyError(f"Unknown policy type rule {rule!r}")
        if not valid:
            raise PolicyError(f"Parameter {key!r} violates rule {rule!r}: {value!r}")

    def _validate_common_sweep(self, parameters: dict[str, Any]) -> None:
        averages = parameters.get("num_averages")
        if averages is not None and averages > int(self.limits["max_num_averages"]):
            raise PolicyError("num_averages exceeds the configured policy limit")
        span = parameters.get("frequency_span_in_mhz")
        step = parameters.get("frequency_step_in_mhz")
        if span is not None and step is not None:
            points = math.floor(float(span) / float(step)) + 1
            if points > int(self.limits["max_sweep_points"]):
                raise PolicyError(
                    f"Frequency sweep has {points} points; policy maximum is "
                    f"{self.limits['max_sweep_points']}"
                )
        if parameters.get("simulate") and parameters.get("load_data_id") is not None:
            raise PolicyError("simulate and load_data_id cannot be used together")

    def _validate_node_specific(
        self, node_id: str, parameters: dict[str, Any], state: dict[str, Any]
    ) -> None:
        qubits = parameters["qubits"]
        if node_id in {"02x", "02a", "02c"}:
            span_mhz = float(parameters["frequency_span_in_mhz"])
            resonator_rules = self.raw.get("analysis", {}).get("resonator", {})
            max_span_mhz = float(resonator_rules.get("max_frequency_span_mhz", 60.0))
            exclusion_hz = float(
                resonator_rules.get("upconverter_exclusion_hz", 1_000_000.0)
            )
            if span_mhz > max_span_mhz:
                raise PolicyError(
                    f"frequency_span_in_mhz {span_mhz:g} exceeds the resonator "
                    f"policy maximum of {max_span_mhz:g} MHz"
                )
            span_hz = span_mhz * 1e6
            limit = float(self.limits["resonator_if_abs_hz"])
            for name in qubits:
                current_if = state["qubits"][name]["resonator"]["intermediate_frequency"]
                if not is_finite_number(current_if):
                    raise PolicyError(f"{name} resonator IF is not numeric")
                if abs(float(current_if)) + span_hz / 2 > limit:
                    raise PolicyError(
                        f"{name} resonator sweep exceeds ±{limit / 1e6:g} MHz IF"
                    )
                if abs(float(current_if)) <= span_hz / 2 + exclusion_hz:
                    raise PolicyError(
                        f"{name} resonator sweep includes the readout "
                        "upconverter frequency; reduce frequency_span_in_mhz or "
                        "move resonator IF farther from the LO"
                    )

        if node_id == "02c":
            if parameters["min_power_dbm"] >= parameters["max_power_dbm"]:
                raise PolicyError("min_power_dbm must be lower than max_power_dbm")
            full_scale = self.limits["opx1000_full_scale_power_dbm"]
            if parameters["max_power_dbm"] > full_scale["max"]:
                raise PolicyError("max_power_dbm exceeds the OPX1000 policy limit")
            if parameters["min_power_dbm"] < -80:
                raise PolicyError("min_power_dbm is below the policy floor")
            if parameters["max_amp"] > 1:
                raise PolicyError("max_amp cannot exceed normalized amplitude 1")
            if parameters["num_power_points"] > self.limits["max_sweep_points"]:
                raise PolicyError("num_power_points exceeds the sweep-point limit")
            resonator_02c = self.raw.get("analysis", {}).get("02c", {})
            max_power_points = int(resonator_02c.get("max_num_power_points", 30))
            max_averages = int(resonator_02c.get("max_num_averages", 100))
            if int(parameters["num_power_points"]) > max_power_points:
                raise PolicyError(
                    f"num_power_points {parameters['num_power_points']} exceeds "
                    f"the 02c policy maximum of {max_power_points}"
                )
            if int(parameters["num_averages"]) > max_averages:
                raise PolicyError(
                    f"num_averages {parameters['num_averages']} exceeds "
                    f"the 02c policy maximum of {max_averages}"
                )

        if node_id == "03a":
            span_hz = float(parameters["frequency_span_in_mhz"]) * 1e6
            limit = float(self.limits["qubit_if_abs_hz"])
            arbitrary_frequency = parameters.get("arbitrary_qubit_frequency_in_ghz")
            for name in qubits:
                qubit = state["qubits"][name]
                if arbitrary_frequency is not None:
                    # 03a_Qubit_Spectroscopy.py computes
                    # sqrt(detuning / freq_vs_flux_01_quad_term) for an
                    # arbitrary center. That term is still zero until flux
                    # spectroscopy has run, and the node raises
                    # ZeroDivisionError mid-run; see the 2026-09-19 lesson.
                    quad_term = qubit.get("freq_vs_flux_01_quad_term")
                    if not is_finite_number(quad_term) or float(quad_term) == 0:
                        raise PolicyError(
                            f"{name} freq_vs_flux_01_quad_term is zero, so "
                            "arbitrary_qubit_frequency_in_ghz would divide by "
                            "zero inside the node; use "
                            "jy_request_03a_candidate_center or "
                            "jy_request_03a_window_shift to move the scan"
                        )
                current_if = qubit["xy"]["intermediate_frequency"]
                if not is_finite_number(current_if):
                    raise PolicyError(f"{name} qubit IF is not numeric")
                if abs(float(current_if)) + span_hz / 2 > limit:
                    raise PolicyError(
                        f"{name} qubit sweep exceeds ±{limit / 1e6:g} MHz IF"
                    )
                if operation_amplitude(qubit, "x180") == 0:
                    raise PolicyError(
                        f"{name} x180 amplitude is zero; approve the bootstrap proposal first"
                    )
                saturation = operation_amplitude(qubit, "saturation")
                kind = channel_kind(qubit)
                maximum = float(self.limits[kind]["max_wf_amplitude"])
                driven = abs(saturation * float(parameters["operation_amplitude_factor"]))
                if driven > maximum:
                    raise PolicyError(
                        f"{name} spectroscopy waveform amplitude {driven:g} exceeds {maximum:g}"
                    )

        if node_id == "04":
            if parameters["min_amp_factor"] >= parameters["max_amp_factor"]:
                raise PolicyError("min_amp_factor must be lower than max_amp_factor")
            points = (
                math.floor(
                    (parameters["max_amp_factor"] - parameters["min_amp_factor"])
                    / parameters["amp_factor_step"]
                )
                + 1
            )
            if points > int(self.limits["max_sweep_points"]):
                raise PolicyError("Power-Rabi amplitude sweep exceeds the point limit")
            operation = parameters["operation_x180_or_any_90"]
            for name in qubits:
                qubit = state["qubits"][name]
                amplitude = operation_amplitude(qubit, operation)
                if amplitude == 0:
                    raise PolicyError(
                        f"{name} {operation} amplitude is zero; bootstrap or calibrate it first"
                    )
                kind = channel_kind(qubit)
                maximum = float(self.limits[kind]["max_wf_amplitude"])
                swept = abs(amplitude) * float(parameters["max_amp_factor"])
                if swept > maximum:
                    raise PolicyError(
                        f"{name} sweep reaches {swept:g}, above {maximum:g}; "
                        "reduce max_amp_factor"
                    )

        if node_id in {"05", "06b"}:
            minimum = parameters["min_wait_time_in_ns"]
            maximum = parameters["max_wait_time_in_ns"]
            step = parameters["wait_time_step_in_ns"]
            if minimum < 16:
                raise PolicyError("min_wait_time_in_ns must be at least 16 ns")
            if any(value % 4 for value in (minimum, maximum, step)):
                raise PolicyError("Decay wait times and step must be multiples of 4 ns")
            if minimum >= maximum:
                raise PolicyError(
                    "min_wait_time_in_ns must be lower than max_wait_time_in_ns"
                )
            points = math.ceil((maximum - minimum) / step)
            if points < 8:
                raise PolicyError("Decay sweep must contain at least 8 points")
            if points > int(self.limits["max_sweep_points"]):
                raise PolicyError("Decay wait-time sweep exceeds the point limit")
            for name in qubits:
                qubit = state["qubits"][name]
                amplitude = operation_amplitude(qubit, "x180")
                if amplitude == 0:
                    raise PolicyError(
                        f"{name} x180 amplitude is zero; calibrate it before "
                        f"{'T1' if node_id == '05' else 'decay measurement'}"
                    )
                kind = channel_kind(qubit)
                amplitude_limit = float(
                    self.limits[kind]["max_x180_wf_amplitude"]
                )
                if abs(amplitude) > amplitude_limit:
                    raise PolicyError(
                        f"{name} x180 amplitude {amplitude:g} exceeds "
                        f"{amplitude_limit:g}"
                    )

        if node_id == "06":
            minimum = parameters["min_wait_time_in_ns"]
            maximum = parameters["max_wait_time_in_ns"]
            if minimum < 16 or minimum >= maximum:
                raise PolicyError("Ramsey wait-time range is invalid")
            if any(value % 4 for value in (minimum, maximum)):
                raise PolicyError("Ramsey wait times must be multiples of 4 ns")
            if parameters["num_time_points"] < 8:
                raise PolicyError("Ramsey sweep must contain at least 8 points")
            if parameters["num_time_points"] > int(self.limits["max_sweep_points"]):
                raise PolicyError("Ramsey wait-time sweep exceeds the point limit")

        if node_id == "07b":
            if parameters["num_runs"] > int(self.limits["max_num_averages"]):
                raise PolicyError("IQ-blob num_runs exceeds the configured policy limit")

        if node_id == "10a":
            depth = parameters["max_circuit_depth"]
            delta = parameters["delta_clifford"]
            if delta < 2 or depth < delta or depth % delta:
                raise PolicyError(
                    "Randomized-benchmarking depth must be divisible by delta_clifford >= 2"
                )
            if depth > 5000 or parameters["num_random_sequences"] > 1000:
                raise PolicyError("Randomized-benchmarking request exceeds the safety limit")

        if node_id in {"05st", "06st_t2star", "06st_t2e"}:
            minimum = parameters["min_wait_time_in_ns"]
            maximum = parameters["max_wait_time_in_ns"]
            if minimum < 16 or minimum >= maximum:
                raise PolicyError("Statistics wait-time range is invalid")
            if any(value % 4 for value in (minimum, maximum)):
                raise PolicyError("Statistics wait times must be multiples of 4 ns")
            if parameters["histo_num"] != 100:
                raise PolicyError("Statistics nodes require exactly histo_num=100")

    def validate_state_patch(
        self, patch: list[dict[str, Any]], base_state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not patch:
            raise PolicyError("State patch cannot be empty")
        config = self.raw["state_commit"]
        patterns = [re.compile(item) for item in config["allowed_json_pointer_patterns"]]
        allowed_ops = set(config["allowed_operations"])
        for item in patch:
            if item.get("op") not in allowed_ops:
                raise PolicyError(f"State patch operation is not allowed: {item}")
            path = item.get("path")
            if not isinstance(path, str) or not any(p.fullmatch(path) for p in patterns):
                raise PolicyError(f"State path is not allowlisted: {path!r}")
            value = item.get("value")
            if isinstance(value, float) and not math.isfinite(value):
                raise PolicyError(f"Non-finite state value at {path}")

        original = base_state or load_state(self.settings.active_state)
        try:
            updated = apply_json_patch(original, patch)
        except Exception as exc:
            raise PolicyError(str(exc)) from exc

        for item in patch:
            path = item["path"]
            value = pointer_get(updated, path)
            if re.fullmatch(
                r"/ports/mw_outputs/[^/]+/[0-9]+/[0-9]+/upconverter_frequency",
                path,
            ):
                limits = self.limits["mw_upconverter_frequency_hz"]
                if (
                    not is_finite_number(value)
                    or not float(limits["min"]) <= float(value) <= float(limits["max"])
                ):
                    raise PolicyError(
                        f"Unsupported MW upconverter frequency at {path}"
                    )
                continue
            parts = path.split("/")
            if len(parts) < 4:
                continue
            name = parts[2]
            qubit = updated["qubits"][name]
            if path.endswith("/intermediate_frequency"):
                limit = (
                    self.limits["qubit_if_abs_hz"]
                    if "/xy/" in path
                    else self.limits["resonator_if_abs_hz"]
                )
                if not is_finite_number(value) or abs(float(value)) > float(limit):
                    raise PolicyError(f"IF update exceeds the limit at {path}")
            elif path.endswith("/amplitude"):
                if not is_finite_number(value):
                    raise PolicyError(f"Amplitude must be numeric at {path}")
                kind = channel_kind(qubit)
                maximum = float(self.limits[kind]["max_wf_amplitude"])
                backing = parts[-2]
                try:
                    x180_backing = operation_backing_name(qubit, "x180")
                except Exception:
                    x180_backing = ""
                if backing == x180_backing:
                    maximum = float(self.limits[kind]["max_x180_wf_amplitude"])
                if abs(float(value)) > maximum:
                    raise PolicyError(
                        f"Amplitude {value} exceeds {maximum} at {path}"
                    )
            elif path.endswith("/full_scale_power_dbm"):
                limits = self.limits["opx1000_full_scale_power_dbm"]
                if not isinstance(value, int) or not limits["min"] <= value <= limits["max"]:
                    raise PolicyError(f"Unsupported full-scale power at {path}")
            elif path.endswith(("/threshold", "/rus_exit_threshold")):
                if not is_finite_number(value):
                    raise PolicyError(f"Readout threshold must be numeric at {path}")
            elif path.endswith("/confusion_matrix"):
                if (
                    not isinstance(value, list)
                    or len(value) != 2
                    or any(not isinstance(row, list) or len(row) != 2 for row in value)
                    or any(
                        not is_finite_number(item) or not 0 <= float(item) <= 1
                        for row in value
                        for item in row
                    )
                ):
                    raise PolicyError(f"Confusion matrix must be a finite 2x2 probability matrix at {path}")
            elif path.endswith("/extras/bare_resonator_freq"):
                if not is_finite_number(value) or float(value) <= 0:
                    raise PolicyError(
                        f"Bare resonator frequency must be finite and positive at {path}"
                    )
            elif path.endswith(("/T1", "/T2ramsey", "/T2echo")) or re.search(
                r"/extras/(T1|T1_dev|T2ramsey|T2ramsey_dev|T2|T2_dev)$", path
            ):
                if not is_finite_number(value) or float(value) <= 0:
                    raise PolicyError(f"Coherence time must be finite and positive at {path}")
            elif path.endswith("/extras/readout_fidelity"):
                if not is_finite_number(value) or not 0 <= float(value) <= 1:
                    raise PolicyError(f"Readout fidelity must be between 0 and 1 at {path}")
        return updated
