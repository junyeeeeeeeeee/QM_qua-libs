from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .analysis_02c import (
    analyze_02c_transitions,
    node_selected_readout_powers_dbm,
)
from .config import Settings
from .policy import PolicyEngine
from .state_patch_02c import derive_02c_state_patch
from .state import (
    filter_patch,
    json_diff,
    load_state,
    patch_qubit_targets,
    pointer_get,
    recorded_updates_to_patch,
    snapshot_state_path,
    split_patch_by_target,
)
from .util import json_loads, sha256_file


class AnalysisError(RuntimeError):
    pass


def _load_xarray_dataset(path: Path) -> Any:
    """Load Qualibrate datasets even when a NetCDF file uses a .h5 suffix."""
    import xarray as xr

    errors: list[str] = []
    for engine in ("scipy", "h5netcdf", None):
        try:
            if engine is None:
                return xr.load_dataset(path)
            return xr.load_dataset(path, engine=engine)
        except Exception as exc:
            errors.append(f"{engine or 'auto'}: {exc}")
    raise ValueError("; ".join(errors))


class SnapshotAnalyzer:
    """Deterministic evidence extraction; node outcomes are treated as advisory."""

    def __init__(self, settings: Settings, policy: PolicyEngine):
        self.settings = settings
        self.policy = policy

    def analyze_run(self, run: dict[str, Any]) -> dict[str, Any]:
        snapshot_value = run.get("snapshot_path")
        if not snapshot_value:
            raise AnalysisError("Run has no saved snapshot path")
        snapshot = Path(snapshot_value)
        return self.analyze_snapshot(snapshot, str(run["node_id"]), run)

    def analyze_snapshot(
        self,
        snapshot: Path,
        node_id: str,
        run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        node_path = snapshot / "node.json"
        data_path = snapshot / "data.json"
        if not node_path.is_file() or not data_path.is_file():
            return {
                "analysis_status": "failed",
                "failure_reasons": ["Snapshot is missing node.json or data.json."],
                "snapshot_path": str(snapshot),
                "node_id": node_id,
                "plots": [],
            }
        try:
            node_data = json.loads(node_path.read_text(encoding="utf-8"))
            data = json.loads(data_path.read_text(encoding="utf-8"))
            data = _resolve_snapshot_npz_references(snapshot, data)
        except Exception as exc:
            return {
                "analysis_status": "failed",
                "failure_reasons": [f"Snapshot metadata could not be read: {exc}"],
                "snapshot_path": str(snapshot),
                "node_id": node_id,
                "plots": [],
            }

        # Prefer the snapshot's parameters, but fill any missing values from the
        # audited run row so reset qualification never depends on a node choosing
        # to serialize every input into data.json.
        if run is not None:
            recorded_parameters = json_loads(run.get("parameters_json"), {})
            snapshot_parameters = data.get("initial_parameters")
            if not isinstance(snapshot_parameters, dict):
                snapshot_parameters = {}
            data["initial_parameters"] = {
                **recorded_parameters,
                **snapshot_parameters,
            }

        outcomes = node_data.get("data", {}).get("outcomes", {})
        plots = sorted(
            str(path.resolve())
            for path in snapshot.glob("*.png")
            if path.is_file()
        )
        failures: list[str] = []
        warnings: list[str] = []
        if not outcomes:
            warnings.append("Node did not record per-qubit outcomes.")
        failed_outcomes = {
            name: value for name, value in outcomes.items() if value != "successful"
        }
        for name, value in sorted(failed_outcomes.items()):
            # Attribute the node's own per-qubit outcome to that qubit so one
            # failing target cannot suppress a passing target's state patch.
            failures.append(f"{name} node outcome is {value!r}, not successful.")
        if not plots:
            failures.append("No result plot was saved.")

        dataset_metrics = self._dataset_metrics(snapshot, node_id, data)
        if node_id in {"05", "06b"}:
            plots.extend(
                path
                for path in self._selected_decay_plots(
                    snapshot, node_id, dataset_metrics
                )
                if path not in plots
            )
        targets = list(_run_targets(run or {}, data, outcomes))
        stage_03a = self._03a_stage(run) if node_id == "03a" else None
        fit_quality = self._fit_quality(node_id, data, dataset_metrics, targets)
        if node_id == "03a":
            warnings.extend(
                self._normalize_03a_arbitrary_center_fit(
                    fit_quality, dataset_metrics, run
                )
            )
            warnings.extend(
                self._select_03a_fundamental_transition(
                    fit_quality,
                    dataset_metrics,
                    run,
                    stage_03a,
                )
            )
        elif node_id == "04":
            warnings.extend(
                self._normalize_04_fitted_edge_metrics(
                    fit_quality,
                    dataset_metrics,
                    self._04_operation_amplitude_baselines(run, fit_quality),
                )
            )
        failures.extend(fit_quality.pop("failures"))
        if dataset_metrics.get("error"):
            if node_id in {
                "02x",
                "02a",
                "03a",
                "04",
                "05",
                "06b",
            }:
                failures.append(dataset_metrics["error"])
            else:
                warnings.append(dataset_metrics["error"])
        for name, metrics in dataset_metrics.get("qubits", {}).items():
            edge = metrics.get("edge_fraction")
            snr = metrics.get("robust_snr")
            edge_limit = (
                float(self.policy.raw["analysis"]["03a"]["min_edge_fraction"])
                if node_id == "03a"
                else 0.05
            )
            min_snr = (
                float(self.policy.raw["analysis"]["03a"]["min_robust_snr"])
                if node_id == "03a"
                else float(
                    self.policy.raw["analysis"].get(node_id, {}).get(
                        "min_robust_snr", 5.0
                    )
                )
            )
            if (
                node_id in {"02x", "02a", "03a", "04"}
                and isinstance(edge, (int, float))
                and edge < edge_limit
            ):
                failures.append(
                    f"{name} feature lies within {edge_limit:.0%} of a sweep edge."
                )
            if node_id in {"02x", "02a", "03a", "04", "05"}:
                if not _finite(snr):
                    failures.append(f"{name} robust sweep SNR is unavailable.")
                elif float(snr) < min_snr:
                    failures.append(
                        f"{name} robust sweep SNR {float(snr):.3g} is below "
                        f"{min_snr:g}."
                    )
            if node_id == "03a" and stage_03a == "fine":
                width = metrics.get("feature_fwhm_hz")
                min_width = 1e6 * float(
                    self.policy.raw["analysis"]["03a"][
                        "final_min_feature_fwhm_mhz"
                    ]
                )
                max_width = 1e6 * float(
                    self.policy.raw["analysis"]["03a"][
                        "final_max_feature_fwhm_mhz"
                    ]
                )
                if not _finite(width):
                    failures.append(f"{name} feature FWHM could not be measured.")
                elif not min_width <= float(width) <= max_width:
                    failures.append(
                        f"{name} feature FWHM {float(width) / 1e6:.3g} MHz is "
                        f"outside the required {min_width / 1e6:g}-"
                        f"{max_width / 1e6:g} MHz range."
                    )

        if node_id in {"02x", "02a"}:
            resonator_rules = self.policy.raw.get("analysis", {}).get("resonator", {})
            exclusion_hz = float(
                resonator_rules.get("upconverter_exclusion_hz", 1_000_000.0)
            )
            try:
                current_for_lo = load_state(self.settings.active_state)
            except Exception:
                current_for_lo = {}
            for name, metrics in dataset_metrics.get("qubits", {}).items():
                if not isinstance(metrics, dict):
                    continue
                if metrics.get("sweep_dimension") not in {None, "freq"}:
                    continue
                feature = metrics.get("feature_coordinate")
                if not _finite(feature):
                    continue
                current_if = (
                    current_for_lo.get("qubits", {})
                    .get(name, {})
                    .get("resonator", {})
                    .get("intermediate_frequency")
                )
                if not _finite(current_if):
                    continue
                # Dataset `freq` is detuning from resonator RF. The LO/upconverter
                # artifact sits at detuning -IF.
                if abs(float(feature) + float(current_if)) <= exclusion_hz:
                    failures.append(
                        f"{name} selected feature is within {exclusion_hz / 1e6:g} MHz "
                        "of the readout upconverter frequency and must not be used."
                    )

        candidate_patch: list[dict[str, Any]] = []
        rejected_patch: list[dict[str, Any]] = []
        snapshot_state = snapshot_state_path(snapshot)
        active_hash = sha256_file(self.settings.active_state)
        if run is None:
            warnings.append(
                "Historical replay does not derive a state patch because its original "
                "active-state baseline is unavailable."
            )
        elif snapshot_state is not None:
            current = load_state(self.settings.active_state)
            before_hash = run.get("active_state_hash_before")
            if before_hash and active_hash != before_hash:
                warnings.append(
                    "Active state changed after the run; candidate state patch is "
                    "suppressed as stale."
                )
            else:
                proposed = load_state(snapshot_state)
                changes_by_path = {
                    item["path"]: item for item in json_diff(current, proposed)
                }
                updates_path = (
                    self.settings.runtime
                    / "requests"
                    / f"{run.get('id')}.state_updates.json"
                )
                if updates_path.is_file():
                    try:
                        recorded = json.loads(updates_path.read_text(encoding="utf-8"))
                        recorded_patch = recorded_updates_to_patch(recorded, current)
                        recorded_patch = self._normalize_chained_frequency_updates(
                            recorded_patch, node_id, current
                        )
                    except Exception as exc:
                        failures.append(
                            f"Recorded Qualibrate state updates are invalid: {exc}"
                        )
                    else:
                        for item in recorded_patch:
                            changes_by_path[item["path"]] = item
                        warnings.append(
                            f"Loaded {len(recorded_patch)} recorded Qualibrate state updates."
                        )
                all_changes = list(changes_by_path.values())
                target_changes, unrelated = self._target_node_changes(
                    all_changes,
                    node_id,
                    targets,
                )
                candidate_patch, policy_rejected = filter_patch(
                    target_changes,
                    self.policy.raw["state_commit"][
                        "allowed_json_pointer_patterns"
                    ],
                )
                rejected_patch = unrelated + policy_rejected
                if node_id == "02c":
                    candidate_patch, jy_patch_errors = derive_02c_state_patch(
                        self.settings,
                        current,
                        dataset_metrics,
                        targets,
                    )
                    failures.extend(jy_patch_errors)
                    warnings.append(
                        "02c candidate updates are derived from JY dressed-plateau analysis; protected-node fit points are ignored."
                    )
                if node_id == "03a" and stage_03a != "fine":
                    candidate_patch = []
                    warnings.append(
                        "03a coarse/refinement fits are candidate frequencies only; "
                        "state updates are suppressed until a final lower-power "
                        "narrow fine scan passes."
                    )
                elif node_id == "03a":
                    candidate_patch, normalization_warnings = (
                        self._normalize_03a_arbitrary_center_patch(
                            candidate_patch,
                            fit_quality,
                            current,
                            load_state(self.settings.wiring_path),
                            targets,
                            run,
                        )
                    )
                    warnings.extend(normalization_warnings)
                elif node_id in {"05", "06b"}:
                    candidate_patch, normalization_warnings = (
                        self._normalize_decay_quadrature_patch(
                            candidate_patch,
                            fit_quality,
                            node_id,
                        )
                    )
                    warnings.extend(normalization_warnings)
                elif node_id == "07b":
                    candidate_patch, normalization_warnings = (
                        self._normalize_07b_fidelity_patch(
                            candidate_patch, fit_quality
                        )
                    )
                    warnings.extend(normalization_warnings)
                if candidate_patch:
                    try:
                        self.policy.validate_state_patch(candidate_patch, current)
                    except Exception as exc:
                        failures.append(
                            f"Candidate state update violates policy: {exc}"
                        )
                if rejected_patch:
                    warnings.append(
                        f"{len(rejected_patch)} snapshot/recorded changes are outside this "
                        "node/target state allowlist and will not be proposed."
                    )
        else:
            warnings.append(
                "Snapshot has no saved QuAM state; no state patch can be derived."
            )

        status = "pass" if not failures else "needs_review"
        failures_by_target, run_level_failures = _attribute_failures(
            failures, targets
        )
        passing_targets = (
            []
            if run_level_failures
            else sorted(
                str(name) for name in targets if str(name) not in failures_by_target
            )
        )
        suppressed_targets: list[str] = []
        if status != "pass" and candidate_patch:
            # Operator instruction 2026-09-20: a run that is `needs_review`
            # overall must still commit the qubits that individually passed
            # every check. Suppressing the whole patch lost calibrated values
            # permanently, because the node then refused a further run for an
            # already-resolved target. Only passing evidence is ever kept.
            candidate_patch, dropped = split_patch_by_target(
                candidate_patch, set(passing_targets)
            )
            suppressed_targets = sorted(
                patch_qubit_targets(dropped) - set(passing_targets)
            )
            if suppressed_targets:
                warnings.append(
                    f"{node_id} state updates are suppressed for "
                    f"{suppressed_targets} because those targets did not pass "
                    "every fit, SNR, morphology, and sweep-edge check."
                )
            unattributed = [
                str(item.get("path", ""))
                for item in dropped
                if not re.match(r"^/qubits/q[0-9]+/", str(item.get("path", "")))
            ]
            if unattributed:
                warnings.append(
                    f"{node_id} state updates are suppressed for "
                    f"{unattributed} because a non-passing run may commit only "
                    "changes that belong to an individually passing qubit."
                )
            if run_level_failures:
                warnings.append(
                    f"{node_id} state updates are suppressed for every target "
                    "because this run has failures that belong to the run as a "
                    f"whole: {run_level_failures}"
                )
        return {
            "analysis_status": status,
            "node_id": node_id,
            "snapshot_id": node_data.get("id"),
            "snapshot_path": str(snapshot.resolve()),
            "plots": plots,
            "outcomes_advisory": outcomes,
            "fit_quality": fit_quality,
            "03a_stage": stage_03a,
            "dataset_metrics": dataset_metrics,
            "failure_reasons": failures,
            "failure_reasons_by_target": failures_by_target,
            "run_level_failure_reasons": run_level_failures,
            "passing_targets": passing_targets,
            "suppressed_patch_targets": suppressed_targets,
            "warnings": warnings,
            "candidate_state_patch": candidate_patch,
            "rejected_state_change_count": len(rejected_patch),
            "active_state_hash": active_hash,
        }

    @staticmethod
    def _normalize_04_fitted_edge_metrics(
        fit_quality: dict[str, Any],
        dataset_metrics: dict[str, Any],
        amplitude_baselines: dict[str, float],
    ) -> list[str]:
        """Use fitted Pi factors, not noisy raw extrema, for 04 edge checks."""
        results = fit_quality.get("results", {})
        metrics_by_qubit = dataset_metrics.get("qubits", {})
        if not isinstance(results, dict) or not isinstance(metrics_by_qubit, dict):
            return []
        warnings: list[str] = []
        for name, fit in results.items():
            metrics = metrics_by_qubit.get(name)
            if not isinstance(fit, dict) or not isinstance(metrics, dict):
                continue
            pi_amplitude = fit.get("Pi_amplitude")
            baseline = amplitude_baselines.get(str(name))
            sweep_min = metrics.get("sweep_min_coordinate")
            sweep_max = metrics.get("sweep_max_coordinate")
            if not all(
                _finite(value)
                for value in (pi_amplitude, baseline, sweep_min, sweep_max)
            ) or float(baseline) == 0:
                continue
            span = float(sweep_max) - float(sweep_min)
            if span <= 0:
                continue
            fitted_factor = float(pi_amplitude) / float(baseline)
            fitted_edge = min(
                fitted_factor - float(sweep_min),
                float(sweep_max) - fitted_factor,
            ) / span
            previous_coordinate = metrics.get("feature_coordinate")
            previous_edge = metrics.get("edge_fraction")
            metrics["raw_extremum_coordinate"] = previous_coordinate
            metrics["raw_extremum_edge_fraction"] = previous_edge
            metrics["feature_coordinate"] = fitted_factor
            metrics["edge_fraction"] = float(fitted_edge)
            metrics["fitted_pi_amplitude"] = float(pi_amplitude)
            metrics["operation_amplitude_before_run"] = float(baseline)
            metrics["edge_evidence_source"] = "fitted_Pi_amplitude_factor"
            warnings.append(
                f"{name} 04 edge evidence uses fitted Pi factor "
                f"{fitted_factor:.9g} from Pi amplitude {float(pi_amplitude):.9g} "
                f"and recorded pre-run operation amplitude {float(baseline):.9g}."
            )
        return warnings

    def _04_operation_amplitude_baselines(
        self,
        run: dict[str, Any] | None,
        fit_quality: dict[str, Any],
    ) -> dict[str, float]:
        """Read pre-run operation amplitudes from the worker's audit record."""
        if run is None or not run.get("id"):
            return {}
        updates_path = (
            self.settings.runtime
            / "requests"
            / f"{run.get('id')}.state_updates.json"
        )
        if not updates_path.is_file():
            return {}
        try:
            updates = json.loads(updates_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        results = fit_quality.get("results", {})
        if not isinstance(updates, dict) or not isinstance(results, dict):
            return {}
        baselines: dict[str, float] = {}
        for name, fit in results.items():
            if not isinstance(fit, dict) or not _finite(fit.get("Pi_amplitude")):
                continue
            pi_amplitude = float(fit["Pi_amplitude"])
            prefix = f"#/qubits/{name}/xy/operations/"
            for key, update in updates.items():
                if not str(key).startswith(prefix) or not isinstance(update, dict):
                    continue
                old = update.get("old")
                new = update.get("new")
                if (
                    _finite(old)
                    and float(old) != 0
                    and _finite(new)
                    and math.isclose(float(new), pi_amplitude, rel_tol=1e-9, abs_tol=1e-12)
                ):
                    baselines[str(name)] = float(old)
                    break
        return baselines

    def _select_03a_fundamental_transition(
        self,
        fit_quality: dict[str, Any],
        dataset_metrics: dict[str, Any],
        run: dict[str, Any] | None,
        stage_03a: str | None,
    ) -> list[str]:
        """Prefer the higher, broader |0>-|1> peak over a lower two-photon peak."""
        if stage_03a != "coarse_candidate":
            return []
        parameters = self._03a_parameters(run)
        amplitude = parameters.get("operation_amplitude_factor")
        rules = self.policy.raw["analysis"]["03a"]
        if not _finite(amplitude) or float(amplitude) < float(
            rules["two_photon_detection_min_operation_amplitude_factor"]
        ):
            return []
        results = fit_quality.get("results", {})
        metrics_by_qubit = dataset_metrics.get("qubits", {})
        if not isinstance(results, dict) or not isinstance(metrics_by_qubit, dict):
            return []
        warnings: list[str] = []
        for name, metrics in metrics_by_qubit.items():
            if not isinstance(metrics, dict):
                continue
            pair = _select_higher_transition_pair(
                metrics.get("peak_candidates"),
                min_separation_hz=1e6
                * float(rules["two_photon_pair_min_separation_mhz"]),
                max_separation_hz=1e6
                * float(rules["two_photon_pair_max_separation_mhz"]),
                lower_max_width_ratio=float(
                    rules["two_photon_lower_max_width_ratio"]
                ),
                min_peak_snr=float(rules["two_photon_min_peak_snr"]),
            )
            if pair is None:
                continue
            lower = pair["lower_two_photon_candidate"]
            upper = pair["upper_fundamental_candidate"]
            original_coordinate = metrics.get("feature_coordinate")
            selected_coordinate = upper["coordinate_hz"]
            metrics["two_photon_transition_pair"] = pair
            metrics["original_feature_coordinate"] = original_coordinate
            metrics["feature_coordinate"] = selected_coordinate
            metrics["feature_fwhm_hz"] = upper["fwhm_hz"]
            metrics["robust_snr"] = upper["robust_snr"]
            metrics["edge_fraction"] = upper["edge_fraction"]
            metrics["dynamic_range"] = upper["prominence"]
            metrics["feature_selection"] = "higher_frequency_fundamental"
            fit = results.get(name)
            if (
                isinstance(fit, dict)
                and _finite(fit.get("drive_freq"))
                and _finite(original_coordinate)
            ):
                center_frequency = float(fit["drive_freq"]) - float(
                    original_coordinate
                )
                fit["protected_node_drive_freq"] = fit["drive_freq"]
                fit["drive_freq"] = center_frequency + float(selected_coordinate)
                fit["transition_selection"] = "higher_frequency_fundamental"
            warnings.append(
                f"{name} lower peak at {float(lower['coordinate_hz']) / 1e6:.3g} MHz "
                f"is narrower than the peak {float(pair['separation_hz']) / 1e6:.3g} MHz "
                "above it; selected the higher-frequency peak as the |0>-|1> candidate."
            )
        return warnings

    def _03a_stage(self, run: dict[str, Any] | None) -> str:
        parameters: dict[str, Any] = {}
        if isinstance(run, dict):
            direct = run.get("parameters")
            if isinstance(direct, dict):
                parameters = direct
            else:
                parameters = json_loads(run.get("parameters_json"), {})
        rules = self.policy.raw["analysis"]["03a"]
        span = parameters.get("frequency_span_in_mhz")
        amplitude = parameters.get("operation_amplitude_factor")
        if (
            _finite(span)
            and float(span) <= float(rules["final_max_span_mhz"])
            and _finite(amplitude)
            and float(amplitude)
            <= float(rules["final_max_operation_amplitude_factor"])
        ):
            return "fine"
        if (
            _finite(span)
            and float(span) <= float(rules["refinement_max_span_mhz"])
            and _finite(amplitude)
            and float(amplitude)
            <= float(rules["refinement_max_operation_amplitude_factor"])
        ):
            return "refinement_candidate"
        return "coarse_candidate"

    @staticmethod
    def _03a_parameters(run: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(run, dict):
            return {}
        direct = run.get("parameters")
        if isinstance(direct, dict):
            return direct
        return json_loads(run.get("parameters_json"), {})

    @classmethod
    def _normalize_03a_arbitrary_center_fit(
        cls,
        fit_quality: dict[str, Any],
        dataset_metrics: dict[str, Any],
        run: dict[str, Any] | None,
    ) -> list[str]:
        """Translate 03a feature offsets from an explicit sweep center to RF."""
        parameters = cls._03a_parameters(run)
        center_ghz = parameters.get("arbitrary_qubit_frequency_in_ghz")
        if not _finite(center_ghz):
            return []
        center_hz = float(center_ghz) * 1e9
        results = fit_quality.get("results", {})
        metrics_by_name = dataset_metrics.get("qubits", {})
        if not isinstance(results, dict) or not isinstance(metrics_by_name, dict):
            return []
        warnings: list[str] = []
        for name, result in results.items():
            metrics = metrics_by_name.get(name, {})
            offset = (
                metrics.get("feature_coordinate")
                if isinstance(metrics, dict)
                else None
            )
            if not isinstance(result, dict) or not _finite(offset):
                continue
            corrected = center_hz + float(offset)
            recorded = result.get("drive_freq")
            result["drive_freq"] = corrected
            if _finite(recorded) and not math.isclose(
                float(recorded), corrected, abs_tol=1.0
            ):
                warnings.append(
                    f"{name} protected-node drive frequency "
                    f"{float(recorded):.9g} Hz was normalized to explicit-center "
                    f"sweep RF {corrected:.9g} Hz."
                )
        return warnings

    @classmethod
    def _normalize_03a_arbitrary_center_patch(
        cls,
        patch: list[dict[str, Any]],
        fit_quality: dict[str, Any],
        current: dict[str, Any],
        wiring: dict[str, Any],
        targets: list[str],
        run: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Keep fine-scan state patches consistent with an explicit RF center."""
        parameters = cls._03a_parameters(run)
        if not _finite(parameters.get("arbitrary_qubit_frequency_in_ghz")):
            return patch, []
        by_path = {item["path"]: dict(item) for item in patch}
        results = fit_quality.get("results", {})
        wiring_qubits = wiring.get("wiring", {}).get("qubits", {})
        warnings: list[str] = []
        for name in targets:
            result = results.get(name, {}) if isinstance(results, dict) else {}
            frequency = result.get("drive_freq") if isinstance(result, dict) else None
            if not _finite(frequency):
                continue
            output_ref = (
                wiring_qubits.get(name, {}).get("xy", {}).get("opx_output")
                if isinstance(wiring_qubits, dict)
                else None
            )
            if not isinstance(output_ref, str):
                output_ref = (
                    current.get("qubits", {})
                    .get(name, {})
                    .get("xy", {})
                    .get("opx_output")
                )
            if not isinstance(output_ref, str) or not output_ref.startswith(
                "#/ports/"
            ):
                raise AnalysisError(
                    f"Cannot resolve XY LO for {name} fine-scan patch"
                )
            lo = pointer_get(current, f"{output_ref[1:]}/upconverter_frequency")
            if not _finite(lo):
                raise AnalysisError(f"Cannot resolve numeric XY LO for {name}")
            rf_hz = float(frequency)
            replacements = {
                f"/qubits/{name}/extras/idle_freq": rf_hz,
                f"/qubits/{name}/xy/intermediate_frequency": rf_hz - float(lo),
            }
            changed = False
            for path, value in replacements.items():
                if path in by_path:
                    by_path[path]["value"] = value
                    changed = True
            if changed:
                warnings.append(
                    f"{name} fine-scan frequency patch was normalized to "
                    "explicit-center RF."
                )
        return list(by_path.values()), warnings

    @staticmethod
    def _normalize_decay_quadrature_patch(
        patch: list[dict[str, Any]],
        fit_quality: dict[str, Any],
        node_id: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Use the JY-selected decay quadrature instead of the node's fixed I fit."""
        results = fit_quality.get("results", {})
        if not isinstance(results, dict):
            return patch, []
        state_name = "T1" if node_id == "05" else "T2echo"
        fit_name = "t1_seconds" if node_id == "05" else "coherence_seconds"
        normalized: list[dict[str, Any]] = []
        warnings: list[str] = []
        pattern = re.compile(rf"^/qubits/(q[0-9]+)/{state_name}$")
        for raw_item in patch:
            item = dict(raw_item)
            match = pattern.fullmatch(str(item.get("path", "")))
            fit = results.get(match.group(1)) if match else None
            selected_value = fit.get(fit_name) if isinstance(fit, dict) else None
            if _finite(selected_value) and float(selected_value) > 0:
                item["value"] = float(selected_value)
                signal = str(fit.get("signal") or "unknown")
                if signal not in {"I", "state"}:
                    warnings.append(
                        f"{match.group(1)} {state_name} state candidate uses the "
                        f"JY-selected {signal} quadrature rather than the protected "
                        "node's fixed I fit."
                    )
            normalized.append(item)
        return normalized, warnings

    @staticmethod
    def _normalize_chained_frequency_updates(
        patch: list[dict[str, Any]],
        node_id: str,
        current: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Resolve RF values that depend on an IF update recorded in the same block."""
        path_kinds = {
            "02a": ("resonator/intermediate_frequency", "dressed_resonator_freq"),
            "02c": ("resonator/intermediate_frequency", "dressed_resonator_freq"),
            "03a": ("xy/intermediate_frequency", "idle_freq"),
            "06": ("xy/intermediate_frequency", "idle_freq"),
        }
        kinds = path_kinds.get(node_id)
        if kinds is None:
            return patch
        if_suffix, extras_name = kinds
        by_path = {item["path"]: item for item in patch}
        for name in current.get("qubits", {}):
            if_path = f"/qubits/{name}/{if_suffix}"
            rf_path = f"/qubits/{name}/extras/{extras_name}"
            if_item = by_path.get(if_path)
            rf_item = by_path.get(rf_path)
            if if_item is None or rf_item is None:
                continue
            old_if = pointer_get(current, if_path)
            recorded_rf = rf_item.get("value")
            new_if = if_item.get("value")
            values = (old_if, recorded_rf, new_if)
            if not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in values
            ):
                raise AnalysisError(
                    f"Cannot resolve chained frequency update for {name} in {node_id}"
                )
            # Qualibrate's recorder observes the dependent RF value before the
            # recorded IF assignment is applied. Use that observed RF as the
            # baseline: extras may already be stale and are not authoritative.
            rf_item["value"] = float(recorded_rf) + (
                float(new_if) - float(old_if)
            )
        return list(by_path.values())

    def _target_node_changes(
        self,
        changes: list[dict[str, Any]],
        node_id: str,
        targets: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        target_pattern = (
            re.compile(
                r"^/qubits/(" + "|".join(re.escape(name) for name in targets) + r")/"
            )
            if targets
            else re.compile(r"a^")
        )
        node_patterns = {
            "02x": [
                r"^/qubits/q[0-9]+/resonator/intermediate_frequency$",
                r"^/qubits/q[0-9]+/extras/bare_resonator_freq$",
            ],
            "02a": [
                r"^/qubits/q[0-9]+/resonator/intermediate_frequency$",
                r"^/qubits/q[0-9]+/resonator/operations/readout/integration_weights_angle$",
                r"^/qubits/q[0-9]+/extras/dressed_resonator_freq$",
            ],
            "02c": [
                r"^/qubits/q[0-9]+/resonator/intermediate_frequency$",
                r"^/qubits/q[0-9]+/resonator/operations/readout/(amplitude|full_scale_power_dbm)$",
                r"^/qubits/q[0-9]+/extras/dressed_resonator_freq$",
            ],
            # 03a intentionally does not propose x180; bootstrap is a separate action.
            "03a": [
                r"^/qubits/q[0-9]+/xy/intermediate_frequency$",
                r"^/qubits/q[0-9]+/resonator/operations/readout/integration_weights_angle$",
                r"^/qubits/q[0-9]+/extras/idle_freq$",
            ],
            "04": [
                r"^/qubits/q[0-9]+/xy/operations/[A-Za-z0-9_-]+/amplitude$",
            ],
            # 07d optimises readout frequency, duration and power together.
            # Without an entry here every one of its changes is discarded as
            # unrelated and the node produces nothing.
            "07d": [
                r"^/qubits/q[0-9]+/resonator/intermediate_frequency$",
                r"^/qubits/q[0-9]+/resonator/operations/readout/"
                r"(amplitude|full_scale_power_dbm|length)$",
            ],
            "05": [
                r"^/qubits/q[0-9]+/T1$",
            ],
            "07b": [
                r"^/qubits/q[0-9]+/resonator/operations/readout/(integration_weights_angle|threshold|rus_exit_threshold)$",
                r"^/qubits/q[0-9]+/resonator/confusion_matrix$",
                r"^/qubits/q[0-9]+/extras/readout_fidelity$",
            ],
            "06": [
                r"^/qubits/q[0-9]+/xy/intermediate_frequency$",
                r"^/qubits/q[0-9]+/extras/idle_freq$",
                r"^/qubits/q[0-9]+/T2ramsey$",
            ],
            "06b": [
                r"^/qubits/q[0-9]+/T2echo$",
            ],
            "05st": [
                r"^/qubits/q[0-9]+/T1$",
                r"^/qubits/q[0-9]+/extras/(T1|T1_dev)$",
            ],
            "06st_t2star": [
                r"^/qubits/q[0-9]+/T2ramsey$",
                r"^/qubits/q[0-9]+/extras/(T2ramsey|T2ramsey_dev)$",
            ],
            "06st_t2e": [
                r"^/qubits/q[0-9]+/T2echo$",
                r"^/qubits/q[0-9]+/extras/(T2|T2_dev)$",
            ],
        }
        allowed = [re.compile(value) for value in node_patterns.get(node_id, [])]
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for item in changes:
            path = str(item.get("path", ""))
            destination = (
                accepted
                if target_pattern.match(path)
                and any(pattern.fullmatch(path) for pattern in allowed)
                else rejected
            )
            destination.append(item)
        return accepted, rejected

    def _fit_quality(
        self,
        node_id: str,
        data: dict[str, Any],
        dataset_metrics: dict[str, Any] | None = None,
        targets: list[str] | None = None,
    ) -> dict[str, Any]:
        fit_results = data.get("fit_results")
        failures: list[str] = []
        normalized = _json_safe(fit_results)
        if node_id == "03a":
            if not isinstance(fit_results, dict) or not fit_results:
                failures.append("03a has no fit_results.")
            else:
                for name in targets or []:
                    if name not in fit_results:
                        failures.append(
                            f"{name} is missing from 03a fit_results."
                        )
                for name, result in fit_results.items():
                    if (
                        not isinstance(result, dict)
                        or result.get("fit_successful") is not True
                    ):
                        failures.append(f"{name} spectroscopy fit was not successful.")
                    frequency = (
                        result.get("drive_freq")
                        if isinstance(result, dict)
                        else None
                    )
                    if not _finite(frequency):
                        failures.append(
                            f"{name} drive frequency is missing or non-finite."
                        )
        elif node_id == "04":
            if not isinstance(fit_results, dict) or not fit_results:
                failures.append("04 has no fit_results.")
            else:
                for name in targets or []:
                    if name not in fit_results:
                        failures.append(
                            f"{name} is missing from 04 fit_results."
                        )
                for name, result in fit_results.items():
                    amplitude = (
                        result.get("Pi_amplitude")
                        if isinstance(result, dict)
                        else None
                    )
                    if not _finite(amplitude) or float(amplitude) == 0:
                        failures.append(
                            f"{name} Pi amplitude is missing, zero, or non-finite."
                        )
        elif node_id == "02c":
            metrics_by_qubit = (dataset_metrics or {}).get("qubits", {})
            target_names = targets or list(metrics_by_qubit)
            jy_results: dict[str, Any] = {}
            for name in target_names:
                transition = (
                    metrics_by_qubit.get(name, {}).get("02c_transition")
                    if isinstance(metrics_by_qubit.get(name), dict)
                    else None
                )
                if not isinstance(transition, dict):
                    failures.append(
                        f"{name} has no dressed/depletion/bare transition analysis."
                    )
                    continue
                if transition.get("skip_subsequent_experiments") is True:
                    jy_results[name] = {
                        "source": "jy_bare_only_absence_rule",
                        "classification": "bare_only_absent",
                        "skip_subsequent_experiments": True,
                    }
                    continue
                transition_failures = transition.get("validation_failures", [])
                for reason in transition_failures:
                    failures.append(f"{name} 02c transition: {reason}.")
                frequency = transition.get("dressed_frequency_hz")
                power = transition.get("dressed_power_limit_dbm")
                if transition_failures:
                    continue
                if not _finite(frequency) or not _finite(power):
                    failures.append(
                        f"{name} JY analysis has no finite dressed frequency/power."
                    )
                    continue
                node_power = transition.get("node_selected_power_dbm")
                if _finite(node_power):
                    transition["protected_node_power_disagreement_db"] = (
                        float(node_power) - float(power)
                    )
                jy_results[name] = {
                    "source": "jy_dressed_plateau_boundary",
                    "RO_frequency": float(frequency),
                    "readout_power_dbm": float(power),
                }
            normalized = _json_safe(jy_results)

        elif node_id == "05":
            metrics_by_qubit = (dataset_metrics or {}).get("qubits", {})
            t1_results: dict[str, Any] = {}
            if not isinstance(metrics_by_qubit, dict) or not metrics_by_qubit:
                failures.append("05 has no usable T1 dataset fit.")
            else:
                for name in targets or []:
                    if name not in metrics_by_qubit:
                        failures.append(f"{name} is missing from the T1 dataset.")
                for name, metrics in metrics_by_qubit.items():
                    if not isinstance(metrics, dict) or metrics.get("error"):
                        message = (
                            metrics.get("error", "invalid fit metrics")
                            if isinstance(metrics, dict)
                            else "invalid fit metrics"
                        )
                        failures.append(f"{name} T1 fit failed: {message}")
                        continue
                    result = {
                        key: metrics.get(key)
                        for key in (
                            "signal",
                            "t1_seconds",
                            "t1_error_seconds",
                            "relative_uncertainty",
                            "r_squared",
                            "coverage_lifetimes",
                            "samples_per_lifetime",
                            "fit_at_search_boundary",
                        )
                    }
                    t1_results[name] = result
                    t1 = result["t1_seconds"]
                    relative_error = result["relative_uncertainty"]
                    r_squared = result["r_squared"]
                    coverage = result["coverage_lifetimes"]
                    samples_per_lifetime = result["samples_per_lifetime"]
                    statistics_ready = (
                        _finite(t1)
                        and float(t1) > 0
                        and _finite(relative_error)
                        and 0 <= float(relative_error) < 0.25
                        and _finite(r_squared)
                        and float(r_squared) >= 0.9
                        and _finite(coverage)
                        and float(coverage) >= 3.5
                        and _finite(samples_per_lifetime)
                        and float(samples_per_lifetime) >= 2
                        and result["fit_at_search_boundary"] is not True
                    )
                    result["fit_successful"] = statistics_ready
                    result["statistics_ready"] = statistics_ready
                    result["recommended_statistics_max_wait_time_in_ns"] = (
                        _recommended_statistics_wait_ns(float(t1))
                        if _finite(t1) and float(t1) > 0
                        else None
                    )
                    if not _finite(t1) or float(t1) <= 0:
                        failures.append(f"{name} T1 is missing, non-finite, or non-positive.")
                    if (
                        not _finite(relative_error)
                        or float(relative_error) < 0
                        or float(relative_error) >= 0.25
                    ):
                        failures.append(f"{name} T1 relative uncertainty is not below 0.25.")
                    if not _finite(r_squared) or float(r_squared) < 0.9:
                        failures.append(f"{name} T1 fit R-squared is below 0.9.")
                    if not _finite(coverage) or float(coverage) < 3.5:
                        failures.append(
                            f"{name} wait-time span covers less than 3.5 fitted T1 lifetimes."
                        )
                    if (
                        not _finite(samples_per_lifetime)
                        or float(samples_per_lifetime) < 2
                    ):
                        failures.append(
                            f"{name} T1 has fewer than two samples per lifetime."
                        )
                    if result["fit_at_search_boundary"] is True:
                        failures.append(f"{name} T1 fit reached its search boundary.")
            normalized = _json_safe(t1_results)
        elif node_id == "07b":
            blob_results = data.get("results")
            rules_07b = self.policy.raw["analysis"]["07b"]
            min_fidelity = float(rules_07b["min_readout_fidelity"])
            active_min_fidelity = float(
                rules_07b["active_reset_min_readout_fidelity"]
            )
            # Operator instruction 2026-09-21: 07b is single-pass, so the
            # compact-cloud rule is no longer a gate at this node and must not
            # silently block active-reset qualification either.
            active_needs_morphology = bool(
                rules_07b.get("active_reset_requires_morphology", True)
            )
            reset_type = _reset_type(data.get("initial_parameters", {}))
            normalized_results: dict[str, Any] = {}
            metrics_by_qubit = (
                dataset_metrics.get("qubits", {})
                if isinstance(dataset_metrics, dict)
                else {}
            )
            if not isinstance(blob_results, dict) or not blob_results:
                failures.append("07b has no discriminator results.")
            else:
                for name in targets or []:
                    result = blob_results.get(name)
                    raw_fidelity = _scalar_number(
                        result.get("fidelity") if isinstance(result, dict) else None
                    )
                    fidelity = raw_fidelity
                    if fidelity is not None and 1.0 < fidelity <= 100.0:
                        fidelity /= 100.0
                    morphology = metrics_by_qubit.get(name, {})
                    morphology_pass = (
                        isinstance(morphology, dict)
                        and morphology.get("morphology_pass") is True
                    )
                    usable_fidelity = (
                        fidelity is not None and min_fidelity <= fidelity <= 1.0
                    )
                    usable = usable_fidelity and morphology_pass
                    active_reset_qualified = (
                        reset_type == "active"
                        and (morphology_pass or not active_needs_morphology)
                        and fidelity is not None
                        and active_min_fidelity <= fidelity <= 1.0
                    )
                    if active_reset_qualified:
                        active_reset_reason = (
                            "Active reset reached at or above "
                            f"{active_min_fidelity:g} fidelity."
                            if not active_needs_morphology
                            else (
                                "Active reset retained compact clouds at or "
                                f"above {active_min_fidelity:g} fidelity."
                            )
                        )
                    elif reset_type != "active":
                        active_reset_reason = (
                            "This 07b run used thermal reset and cannot qualify "
                            "active reset."
                        )
                    elif active_needs_morphology and not morphology_pass:
                        active_reset_reason = (
                            "Active reset did not preserve compact dual-cloud morphology."
                        )
                    else:
                        active_reset_reason = (
                            "Active-reset fidelity is below the qualification floor "
                            f"of {active_min_fidelity:g}."
                        )
                    normalized_results[name] = {
                        "fit_successful": usable,
                        "readout_fidelity": fidelity,
                        "reset_type": reset_type,
                        "active_reset_qualified": active_reset_qualified,
                        "active_reset_fidelity_floor": active_min_fidelity,
                        "active_reset_qualification_reason": active_reset_reason,
                        "protected_node_fidelity": raw_fidelity,
                        "morphology_pass": morphology_pass,
                        "cloud_shape": _json_safe(morphology),
                        "angle": _scalar_number(result.get("angle"))
                        if isinstance(result, dict)
                        else None,
                        "threshold": _scalar_number(result.get("threshold"))
                        if isinstance(result, dict)
                        else None,
                        "confusion_matrix": _json_safe(
                            result.get("confusion_matrix")
                            if isinstance(result, dict)
                            else None
                        ),
                    }
                    if not usable_fidelity:
                        failures.append(
                            f"{name} readout fidelity is missing or below "
                            f"{min_fidelity:g}."
                        )
                    if not morphology_pass:
                        reasons = (
                            morphology.get("morphology_failures", [])
                            if isinstance(morphology, dict)
                            else []
                        )
                        failures.append(
                            f"{name} IQ clouds are not compact round blobs: "
                            + ("; ".join(str(item) for item in reasons) or "shape evidence is missing")
                        )
            normalized = normalized_results
        elif node_id == "06":
            ramsey_results: dict[str, Any] = {}
            parameters = data.get("initial_parameters", {})
            maximum_ns = parameters.get("max_wait_time_in_ns")
            points = parameters.get("num_time_points")
            if not isinstance(fit_results, dict) or not fit_results:
                failures.append("06 has no Ramsey fit_results.")
            else:
                for name in targets or []:
                    raw = fit_results.get(name)
                    decay = _scalar_number(raw.get("decay")) if isinstance(raw, dict) else None
                    error = _scalar_number(raw.get("decay_error")) if isinstance(raw, dict) else None
                    offset = _scalar_number(raw.get("freq_offset")) if isinstance(raw, dict) else None
                    relative = (
                        abs(error / decay)
                        if decay is not None and decay > 0 and error is not None
                        else None
                    )
                    coverage = (
                        float(maximum_ns) * 1e-9 / decay
                        if decay is not None
                        and decay > 0
                        and _finite(maximum_ns)
                        else None
                    )
                    samples = (
                        float(points) / coverage
                        if _finite(points) and _finite(coverage) and float(coverage) > 0
                        else None
                    )
                    usable = (
                        decay is not None
                        and decay > 0
                        and relative is not None
                        and relative < 0.25
                        and coverage is not None
                        and coverage >= 3.5
                        and samples is not None
                        and samples >= 2
                        and offset is not None
                    )
                    ramsey_results[name] = {
                        "fit_successful": usable,
                        "statistics_ready": usable,
                        "coherence_seconds": decay,
                        "coherence_error_seconds": error,
                        "relative_uncertainty": relative,
                        "frequency_offset_hz": offset,
                        "coverage_lifetimes": coverage,
                        "samples_per_lifetime": samples,
                        "recommended_statistics_max_wait_time_in_ns": (
                            _recommended_statistics_wait_ns(decay)
                            if decay is not None and decay > 0
                            else None
                        ),
                    }
                    if not usable:
                        failures.append(
                            f"{name} Ramsey fit is missing, uncertain, or under-covered."
                        )
            normalized = ramsey_results
        elif node_id == "06b":
            metrics_by_qubit = (dataset_metrics or {}).get("qubits", {})
            echo_results: dict[str, Any] = {}
            for name in targets or []:
                metrics = metrics_by_qubit.get(name)
                if not isinstance(metrics, dict) or metrics.get("error"):
                    failures.append(f"{name} T2 echo dataset fit is unavailable.")
                    continue
                result = {
                    key: metrics.get(key)
                    for key in (
                        "signal",
                        "coherence_seconds",
                        "coherence_error_seconds",
                        "relative_uncertainty",
                        "r_squared",
                        "coverage_lifetimes",
                        "samples_per_lifetime",
                        "fit_at_search_boundary",
                    )
                }
                result["fit_successful"] = (
                    _finite(result["coherence_seconds"])
                    and float(result["coherence_seconds"]) > 0
                    and _finite(result["relative_uncertainty"])
                    and float(result["relative_uncertainty"]) < 0.25
                    and _finite(result["r_squared"])
                    and float(result["r_squared"]) >= 0.9
                    and _finite(result["coverage_lifetimes"])
                    and float(result["coverage_lifetimes"]) >= 3.5
                    and _finite(result["samples_per_lifetime"])
                    and float(result["samples_per_lifetime"]) >= 2
                    and result["fit_at_search_boundary"] is not True
                )
                echo_results[name] = result
                result["statistics_ready"] = result["fit_successful"]
                result["recommended_statistics_max_wait_time_in_ns"] = (
                    _recommended_statistics_wait_ns(
                        float(result["coherence_seconds"])
                    )
                    if _finite(result["coherence_seconds"])
                    and float(result["coherence_seconds"]) > 0
                    else None
                )
                if not result["fit_successful"]:
                    failures.append(f"{name} T2 echo fit failed acceptance checks.")
            normalized = _json_safe(echo_results)
        elif node_id == "10a":
            rb_results: dict[str, Any] = {}
            if not isinstance(fit_results, dict) or not fit_results:
                failures.append("10a has no randomized-benchmarking fit_results.")
            else:
                for name in targets or []:
                    raw = fit_results.get(name)
                    epc = _scalar_number(raw.get("EPC")) if isinstance(raw, dict) else None
                    epg = _scalar_number(raw.get("EPG")) if isinstance(raw, dict) else None
                    usable = (
                        isinstance(raw, dict)
                        and raw.get("fit_successful") is True
                        and epc is not None
                        and epg is not None
                        and 0 <= epc <= 1
                        and 0 <= epg <= 1
                    )
                    rb_results[name] = {
                        "fit_successful": usable,
                        "EPC": epc,
                        "EPG": epg,
                        "fit_error": raw.get("fit_error") if isinstance(raw, dict) else None,
                    }
                    if not usable:
                        failures.append(f"{name} randomized-benchmarking fit failed.")
            normalized = rb_results
        elif node_id in {"05st", "06st_t2star", "06st_t2e"}:
            histo_num = data.get("initial_parameters", {}).get("histo_num")
            stats_results: dict[str, Any] = {}
            if histo_num != 100:
                failures.append(f"{node_id} must contain exactly 100 repetitions.")
            for name in targets or []:
                stats_results[name] = {
                    "fit_successful": histo_num == 100,
                    "histo_num": histo_num,
                    "completion_rule": "validated_baselines_then_single_statistics_run",
                    "raw_iteration_refit_required": False,
                }
            normalized = stats_results
        return {"results": normalized, "failures": failures}

    @staticmethod
    def _normalize_07b_fidelity_patch(
        patch: list[dict[str, Any]],
        fit_quality: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Store 07b fidelity as a fraction even when its library returns percent."""
        results = fit_quality.get("results", {})
        if not isinstance(results, dict):
            return patch, []
        normalized: list[dict[str, Any]] = []
        warnings: list[str] = []
        pattern = re.compile(r"^/qubits/(q[0-9]+)/extras/readout_fidelity$")
        for original in patch:
            item = dict(original)
            match = pattern.fullmatch(str(item.get("path", "")))
            result = results.get(match.group(1)) if match else None
            fidelity = (
                result.get("readout_fidelity")
                if isinstance(result, dict)
                else None
            )
            if match and _finite(fidelity):
                recorded = item.get("value")
                item["value"] = float(fidelity)
                if _finite(recorded) and not math.isclose(
                    float(recorded), float(fidelity), rel_tol=1e-12, abs_tol=1e-12
                ):
                    warnings.append(
                        f"{match.group(1)} readout fidelity was normalized from "
                        f"{float(recorded):.6g} to {float(fidelity):.6g}."
                    )
            normalized.append(item)
        return normalized, warnings

    def _iq_blob_morphology(self, dataset: Any) -> dict[str, Any]:
        """Measure whether each prepared-state cloud is compact and round.

        Fidelity alone can look acceptable when excessive readout power produces
        a long nonlinear tail. Radius-quantile ratios detect the tail, while the
        covariance-axis ratio rejects strongly elongated clouds.
        """
        import numpy as np

        rules = self.policy.raw["analysis"]["07b"]
        max_axis = float(rules["max_cloud_axis_ratio"])
        max_p95 = float(rules["max_p95_to_median_radius"])
        max_p99 = float(rules["max_p99_to_median_radius"])
        min_samples = int(rules["min_cloud_samples"])
        qubit_names = (
            [str(item) for item in dataset.coords["qubit"].values]
            if "qubit" in dataset.coords
            else []
        )
        metrics: dict[str, Any] = {}
        for name in qubit_names:
            clouds: dict[str, Any] = {}
            failures: list[str] = []
            for label in ("g", "e"):
                i_name, q_name = f"I_{label}", f"Q_{label}"
                if i_name not in dataset.data_vars or q_name not in dataset.data_vars:
                    failures.append(f"{label}-cloud I/Q variables are missing")
                    continue
                i_values = np.asarray(
                    dataset[i_name].sel(qubit=name).values, dtype=float
                ).reshape(-1)
                q_values = np.asarray(
                    dataset[q_name].sel(qubit=name).values, dtype=float
                ).reshape(-1)
                points = np.column_stack((i_values, q_values))
                points = points[np.isfinite(points).all(axis=1)]
                if points.shape[0] < min_samples:
                    failures.append(
                        f"{label}-cloud has {points.shape[0]} samples; requires {min_samples}"
                    )
                    continue
                center = np.median(points, axis=0)
                centered = points - center
                radius = np.linalg.norm(centered, axis=1)
                median_radius, p95_radius, p99_radius = np.percentile(
                    radius, (50, 95, 99)
                )
                if not np.isfinite(median_radius) or median_radius <= 0:
                    failures.append(f"{label}-cloud has zero/non-finite median radius")
                    continue
                covariance = np.cov(centered, rowvar=False)
                eigenvalues = np.linalg.eigvalsh(covariance)
                smallest = max(float(eigenvalues[0]), np.finfo(float).tiny)
                axis_ratio = float(np.sqrt(float(eigenvalues[-1]) / smallest))
                p95_ratio = float(p95_radius / median_radius)
                p99_ratio = float(p99_radius / median_radius)
                cloud_failures: list[str] = []
                if axis_ratio > max_axis:
                    cloud_failures.append(
                        f"axis ratio {axis_ratio:.3g} exceeds {max_axis:g}"
                    )
                if p95_ratio > max_p95:
                    cloud_failures.append(
                        f"p95/median radius {p95_ratio:.3g} exceeds {max_p95:g}"
                    )
                if p99_ratio > max_p99:
                    cloud_failures.append(
                        f"p99/median radius {p99_ratio:.3g} exceeds {max_p99:g}"
                    )
                clouds[label] = {
                    "samples": int(points.shape[0]),
                    "axis_ratio": axis_ratio,
                    "p95_to_median_radius": p95_ratio,
                    "p99_to_median_radius": p99_ratio,
                    "compact_round_blob": not cloud_failures,
                }
                failures.extend(
                    f"{label}-cloud {reason}" for reason in cloud_failures
                )
            metrics[name] = {
                "morphology_pass": not failures and len(clouds) == 2,
                "morphology_failures": failures,
                "clouds": clouds,
            }
        return {
            "qubits": metrics,
            "morphology_rule": {
                "max_cloud_axis_ratio": max_axis,
                "max_p95_to_median_radius": max_p95,
                "max_p99_to_median_radius": max_p99,
                "min_cloud_samples": min_samples,
            },
        }

    @staticmethod
    def _statistics_metadata(
        node_id: str, data: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Expose protected-node summaries as information, never as a gate."""
        summary_key = {
            "05st": "t1_stats",
            "06st_t2star": "t2*_stats",
            "06st_t2e": "t2_stats",
        }[node_id]
        summaries = data.get(summary_key, {}) if isinstance(data, dict) else {}
        return {
            "statistics_node": node_id,
            "validation_mode": "baseline_prerequisites_only",
            "raw_iteration_refit_required": False,
            "protected_node_summaries": _json_safe(summaries),
            "qubits": {},
        }

    def _dataset_metrics(
        self,
        snapshot: Path,
        node_id: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if node_id in {"05st", "06st_t2star", "06st_t2e"}:
            return self._statistics_metadata(node_id, data)
        dataset_path = snapshot / "ds.h5"
        if not dataset_path.is_file():
            return {"error": "Snapshot has no ds.h5 dataset.", "qubits": {}}
        try:
            import numpy as np
            dataset = _load_xarray_dataset(dataset_path)
        except Exception as exc:
            return {"error": f"Dataset could not be loaded: {exc}", "qubits": {}}

        if node_id == "07b":
            return self._iq_blob_morphology(dataset)

        transition_analysis: dict[str, Any] | None = None
        if node_id == "02c":
            rules = self.policy.raw.get("analysis", {}).get("02c", {})
            initial_parameters = (
                data.get("initial_parameters", {})
                if isinstance(data, dict)
                else {}
            )
            context_state_path = snapshot_state_path(snapshot)
            context_state = load_state(
                context_state_path or self.settings.active_state
            )
            qubit_context: dict[str, dict[str, Any]] = {}
            for name, qubit in context_state.get("qubits", {}).items():
                readout = qubit.get("resonator", {}).get("operations", {}).get(
                    "readout", {}
                )
                if isinstance(readout, str) and readout.startswith("#./"):
                    readout = qubit["resonator"]["operations"].get(
                        readout[3:], {}
                    )
                qubit_context[name] = {
                    "expected_bare_frequency_hz": qubit.get("extras", {}).get(
                        "bare_resonator_freq"
                    ),
                    "readout_length_ns": readout.get("length")
                    if isinstance(readout, dict)
                    else None,
                }
            transition_analysis = analyze_02c_transitions(
                dataset,
                rules,
                initial_parameters,
                qubit_context,
            )
            threshold = (
                initial_parameters.get(
                    "derivative_crossing_threshold_in_hz_per_dbm"
                )
                if isinstance(initial_parameters, dict)
                else None
            )
            selected_powers = node_selected_readout_powers_dbm(
                dataset, threshold
            )
            for name, transition in transition_analysis.get("qubits", {}).items():
                transition["node_selected_power_dbm"] = selected_powers.get(name)
        signal_candidates = (
            ("state", "I", "Q", "IQ_abs")
            if node_id in {"05", "06b"}
            else (
                "I_rot" if node_id == "03a" else "",
                "IQ_abs",
                "I",
                "state",
            )
        )
        signal_name = next(
            (
                name
                for name in signal_candidates
                if name and name in dataset.data_vars
            ),
            None,
        )
        if signal_name is None:
            return {
                "error": "No supported signal variable is present.",
                "qubits": {},
            }
        signal = dataset[signal_name]
        sweep_dim = next(
            (
                dim
                for dim in ("freq", "amp", "power_dbm", "idle_time", "time")
                if dim in signal.dims
            ),
            None,
        )
        if sweep_dim is None:
            return {
                "error": "No supported sweep dimension is present.",
                "qubits": {},
            }
        for dim in tuple(signal.dims):
            if dim not in {"qubit", sweep_dim}:
                signal = signal.mean(dim=dim)
        qubit_names = (
            [str(item) for item in dataset.coords["qubit"].values]
            if "qubit" in dataset.coords
            else ["unknown"]
        )
        metrics: dict[str, Any] = {}
        for name in qubit_names:
            selected = signal.sel(qubit=name) if "qubit" in signal.dims else signal
            raw_values = np.asarray(selected.values, dtype=float).reshape(-1)
            coordinate = dataset.coords.get(sweep_dim)
            coordinate_values = (
                np.asarray(coordinate.values, dtype=float).reshape(-1)
                if coordinate is not None and coordinate.ndim == 1
                else np.arange(raw_values.size, dtype=float)
            )
            if coordinate_values.size != raw_values.size:
                metrics[name] = {"error": "Sweep coordinate size does not match data."}
                continue
            finite = np.isfinite(raw_values) & np.isfinite(coordinate_values)
            values = raw_values[finite]
            finite_coordinates = coordinate_values[finite]
            if values.size < 5:
                metrics[name] = {"error": "Too few finite sweep samples."}
                continue
            median = float(np.median(values))
            index = int(np.argmax(np.abs(values - median)))
            edge_fraction = min(index, values.size - 1 - index) / (
                values.size - 1
            )
            differences = np.diff(values)
            noise = float(
                np.median(np.abs(differences - np.median(differences)))
            )
            dynamic_range = float(np.max(values) - np.min(values))
            robust_snr = dynamic_range / max(noise * 1.4826, 1e-15)
            feature_coordinate = None
            if (
                coordinate is not None
                and coordinate.ndim == 1
                and index < finite_coordinates.size
            ):
                feature_coordinate = float(finite_coordinates[index])
            feature_fwhm_hz = None
            peak_candidates: list[dict[str, float]] = []
            if node_id == "03a" and sweep_dim == "freq":
                feature_fwhm_hz = _feature_fwhm(
                    finite_coordinates,
                    values,
                    index,
                )
                rules = self.policy.raw["analysis"]["03a"]
                peak_candidates = _spectroscopy_peak_candidates(
                    finite_coordinates,
                    values,
                    min_distance_hz=1e6
                    * float(rules["two_photon_peak_min_distance_mhz"]),
                )
            metrics[name] = {
                "signal": signal_name,
                "sweep_dimension": sweep_dim,
                "samples": int(values.size),
                "sweep_min_coordinate": float(np.min(finite_coordinates)),
                "sweep_max_coordinate": float(np.max(finite_coordinates)),
                "feature_coordinate": feature_coordinate,
                "feature_fwhm_hz": feature_fwhm_hz,
                "edge_fraction": float(edge_fraction),
                "dynamic_range": dynamic_range,
                "robust_snr": float(robust_snr),
                "peak_candidates": peak_candidates,
            }
            if node_id in {"05", "06b"}:
                fit = _fit_exponential_decay(finite_coordinates, values)
                if fit.get("error"):
                    metrics[name]["error"] = fit["error"]
                else:
                    unit_scale, time_unit = _time_unit_scale(coordinate)
                    tau = float(fit.pop("tau"))
                    tau_error = float(fit.pop("tau_error"))
                    metrics[name].update(
                        {
                            **fit,
                            "fit_time_unit": time_unit,
                            (
                                "t1_seconds"
                                if node_id == "05"
                                else "coherence_seconds"
                            ): tau * unit_scale,
                            (
                                "t1_error_seconds"
                                if node_id == "05"
                                else "coherence_error_seconds"
                            ): tau_error * unit_scale,
                        }
                    )
                decay_candidates = [metrics[name]]
                for alternate_name in ("state", "I", "Q", "IQ_abs"):
                    if (
                        alternate_name == signal_name
                        or alternate_name not in dataset.data_vars
                    ):
                        continue
                    alternate = dataset[alternate_name]
                    for dim in tuple(alternate.dims):
                        if dim not in {"qubit", sweep_dim}:
                            alternate = alternate.mean(dim=dim)
                    if sweep_dim not in alternate.dims:
                        continue
                    alternate_selected = (
                        alternate.sel(qubit=name)
                        if "qubit" in alternate.dims
                        else alternate
                    )
                    alternate_metrics = _decay_trace_metrics(
                        np.asarray(alternate_selected.values, dtype=float).reshape(-1),
                        coordinate_values,
                        coordinate,
                        alternate_name,
                        sweep_dim,
                        node_id,
                    )
                    decay_candidates.append(alternate_metrics)
                metrics[name] = max(decay_candidates, key=_decay_metric_rank)
        result: dict[str, Any] = {"qubits": metrics}
        if transition_analysis is not None:
            for name, transition in transition_analysis.get("qubits", {}).items():
                metrics.setdefault(name, {})["02c_transition"] = transition
            result["02c_rules"] = transition_analysis.get("rules", {})
            if transition_analysis.get("error"):
                result["02c_error"] = transition_analysis["error"]
        return result

    def _selected_decay_plots(
        self,
        snapshot: Path,
        node_id: str,
        dataset_metrics: dict[str, Any],
    ) -> list[str]:
        """Render the quadrature JY actually used when it differs from raw I."""
        selected = dataset_metrics.get("qubits", {})
        if not isinstance(selected, dict):
            return []
        requested = {
            str(metrics.get("signal"))
            for metrics in selected.values()
            if isinstance(metrics, dict)
            and metrics.get("signal") not in {None, "I", "state"}
        }
        if not requested:
            return []
        try:
            import matplotlib

            matplotlib.use("Agg")
            from matplotlib import pyplot as plt
            import numpy as np
            dataset = _load_xarray_dataset(snapshot / "ds.h5")
        except Exception:
            return []
        output_root = self.settings.runtime / "report_assets"
        output_root.mkdir(parents=True, exist_ok=True)
        rendered: list[str] = []
        for name, metrics in selected.items():
            if not isinstance(metrics, dict):
                continue
            signal_name = str(metrics.get("signal"))
            if signal_name not in requested or signal_name not in dataset.data_vars:
                continue
            signal = dataset[signal_name]
            if "qubit" in signal.dims:
                try:
                    signal = signal.sel(qubit=name)
                except Exception:
                    continue
            sweep_dim = str(metrics.get("sweep_dimension") or "idle_time")
            if sweep_dim not in signal.dims or sweep_dim not in dataset.coords:
                continue
            for dim in tuple(signal.dims):
                if dim != sweep_dim:
                    signal = signal.mean(dim=dim)
            x = np.asarray(dataset.coords[sweep_dim].values, dtype=float).reshape(-1)
            y = np.asarray(signal.values, dtype=float).reshape(-1)
            finite = np.isfinite(x) & np.isfinite(y)
            x = x[finite]
            y = y[finite]
            if x.size < 5:
                continue
            fit = _fit_exponential_decay(x, y)
            if fit.get("error"):
                continue
            tau = float(fit["tau"])
            amplitude = float(fit["decay_amplitude"])
            offset = float(fit["decay_offset"])
            fitted = amplitude * np.exp(-(x - x[0]) / tau) + offset
            fig, axis = plt.subplots(figsize=(6.4, 4.2))
            axis.plot(x, y, color="#1677b3", linewidth=1.1, label=f"{signal_name} data")
            axis.plot(x, fitted, "r--", linewidth=1.6, label="JY fit")
            axis.set_title(f"{node_id} {name} — JY selected {signal_name}")
            axis.set_xlabel(f"{sweep_dim} ({metrics.get('fit_time_unit', '')})".strip())
            axis.set_ylabel(signal_name)
            axis.legend(loc="best")
            axis.grid(alpha=0.2)
            axis.text(
                0.02,
                0.04,
                f"tau={tau:.3g}, R²={float(fit['r_squared']):.4f}",
                transform=axis.transAxes,
                bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none"},
            )
            fig.tight_layout()
            safe_snapshot = re.sub(r"[^A-Za-z0-9_.-]+", "_", snapshot.name)
            safe_signal = re.sub(r"[^A-Za-z0-9_.-]+", "_", signal_name)
            output = output_root / f"{safe_snapshot}-{node_id}-{name}-{safe_signal}.png"
            fig.savefig(output, dpi=150)
            plt.close(fig)
            rendered.append(str(output.resolve()))
        return rendered


def _decay_trace_metrics(
    raw_values: Any,
    coordinate_values: Any,
    coordinate: Any,
    signal_name: str,
    sweep_dim: str,
    node_id: str,
) -> dict[str, Any]:
    """Fit one decay quadrature and return metrics comparable across signals."""
    import numpy as np

    raw_values = np.asarray(raw_values, dtype=float).reshape(-1)
    coordinate_values = np.asarray(coordinate_values, dtype=float).reshape(-1)
    if coordinate_values.size != raw_values.size:
        return {
            "signal": signal_name,
            "error": "Sweep coordinate size does not match data.",
        }
    finite = np.isfinite(raw_values) & np.isfinite(coordinate_values)
    values = raw_values[finite]
    finite_coordinates = coordinate_values[finite]
    if values.size < 5:
        return {"signal": signal_name, "error": "Too few finite sweep samples."}
    median = float(np.median(values))
    index = int(np.argmax(np.abs(values - median)))
    differences = np.diff(values)
    noise = float(np.median(np.abs(differences - np.median(differences))))
    dynamic_range = float(np.max(values) - np.min(values))
    result: dict[str, Any] = {
        "signal": signal_name,
        "sweep_dimension": sweep_dim,
        "samples": int(values.size),
        "sweep_min_coordinate": float(np.min(finite_coordinates)),
        "sweep_max_coordinate": float(np.max(finite_coordinates)),
        "feature_coordinate": float(finite_coordinates[index]),
        "feature_fwhm_hz": None,
        "edge_fraction": float(
            min(index, values.size - 1 - index) / (values.size - 1)
        ),
        "dynamic_range": dynamic_range,
        "robust_snr": dynamic_range / max(noise * 1.4826, 1e-15),
        "peak_candidates": [],
    }
    fit = _fit_exponential_decay(finite_coordinates, values)
    if fit.get("error"):
        result["error"] = fit["error"]
        return result
    unit_scale, time_unit = _time_unit_scale(coordinate)
    tau = float(fit.pop("tau"))
    tau_error = float(fit.pop("tau_error"))
    result.update(
        {
            **fit,
            "fit_time_unit": time_unit,
            ("t1_seconds" if node_id == "05" else "coherence_seconds"): (
                tau * unit_scale
            ),
            (
                "t1_error_seconds"
                if node_id == "05"
                else "coherence_error_seconds"
            ): tau_error * unit_scale,
        }
    )
    return result


def _decay_metric_rank(metrics: dict[str, Any]) -> tuple[int, int, float, float]:
    """Prefer an accepted physical decay, then fit quality and robust SNR."""
    if not isinstance(metrics, dict) or metrics.get("error"):
        return (-1, -1, float("-inf"), float("-inf"))
    lifetime = metrics.get("t1_seconds", metrics.get("coherence_seconds"))
    relative = metrics.get("relative_uncertainty")
    r_squared = metrics.get("r_squared")
    coverage = metrics.get("coverage_lifetimes")
    samples = metrics.get("samples_per_lifetime")
    boundary_free = metrics.get("fit_at_search_boundary") is not True
    accepted = (
        _finite(lifetime)
        and float(lifetime) > 0
        and _finite(relative)
        and 0 <= float(relative) < 0.25
        and _finite(r_squared)
        and float(r_squared) >= 0.9
        and _finite(coverage)
        and float(coverage) >= 3.5
        and _finite(samples)
        and float(samples) >= 2
        and boundary_free
    )
    return (
        int(accepted),
        int(boundary_free),
        float(r_squared) if _finite(r_squared) else float("-inf"),
        float(metrics.get("robust_snr"))
        if _finite(metrics.get("robust_snr"))
        else float("-inf"),
    )


def _recommended_statistics_wait_ns(lifetime_seconds: float) -> int:
    """Use 4.5 lifetimes and round up to a QUA-compatible 4 ns boundary."""
    raw = float(lifetime_seconds) * 4.5e9
    return max(4, int(math.ceil(raw / 4.0) * 4))


def _spectroscopy_peak_candidates(
    coordinates: Any,
    values: Any,
    *,
    min_distance_hz: float,
) -> list[dict[str, float]]:
    """Return reproducible 03a peak candidates with local width and prominence."""
    import numpy as np

    try:
        from scipy.signal import find_peaks, peak_prominences, peak_widths
    except ImportError:
        return []
    x = np.asarray(coordinates, dtype=float).reshape(-1)
    y = np.asarray(values, dtype=float).reshape(-1)
    if x.size != y.size or x.size < 7:
        return []
    order = np.argsort(x)
    x = x[order]
    y = y[order]
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        return []
    steps = np.diff(x)
    positive_steps = np.abs(steps[np.isfinite(steps) & (steps != 0)])
    if positive_steps.size == 0:
        return []
    step_hz = float(np.median(positive_steps))
    distance_points = max(1, int(round(float(min_distance_hz) / step_hz)))
    window = min(5, x.size if x.size % 2 else x.size - 1)
    smooth = y.copy()
    if window >= 3:
        half_window = window // 2
        padded = np.pad(y, (half_window, half_window), mode="edge")
        smooth = np.convolve(padded, np.ones(window) / window, mode="valid")
    noise = float(np.median(np.abs(np.diff(y) - np.median(np.diff(y)))))
    sigma = max(noise * 1.4826, 1e-15)
    baseline = float(np.median(smooth))
    peak_excursion = float(np.max(smooth) - baseline)
    dip_excursion = float(baseline - np.min(smooth))
    sign = 1.0 if peak_excursion >= dip_excursion else -1.0
    profile = sign * (smooth - baseline)
    indices, _ = find_peaks(
        profile,
        distance=distance_points,
        prominence=max(3.0 * sigma, float(np.ptp(profile)) * 0.05),
    )
    if indices.size == 0:
        return []
    prominences = peak_prominences(profile, indices)[0]
    widths = peak_widths(profile, indices, rel_height=0.5)[0] * step_hz
    candidates: list[dict[str, float]] = []
    for index, prominence, width in zip(indices, prominences, widths):
        edge_fraction = min(int(index), x.size - 1 - int(index)) / (x.size - 1)
        candidates.append(
            {
                "coordinate_hz": float(x[int(index)]),
                "fwhm_hz": float(width),
                "prominence": float(prominence),
                "robust_snr": float(prominence / sigma),
                "edge_fraction": float(edge_fraction),
            }
        )
    return sorted(candidates, key=lambda item: item["coordinate_hz"])


def _select_higher_transition_pair(
    candidates: Any,
    *,
    min_separation_hz: float,
    max_separation_hz: float,
    lower_max_width_ratio: float,
    min_peak_snr: float,
) -> dict[str, Any] | None:
    """Identify a narrow lower two-photon peak paired with a broader upper peak."""
    if not isinstance(candidates, list):
        return None
    usable = [
        item
        for item in candidates
        if isinstance(item, dict)
        and all(
            _finite(item.get(key))
            for key in (
                "coordinate_hz",
                "fwhm_hz",
                "prominence",
                "robust_snr",
                "edge_fraction",
            )
        )
        and float(item["fwhm_hz"]) > 0
        and float(item["robust_snr"]) >= float(min_peak_snr)
    ]
    matches: list[tuple[float, dict[str, Any]]] = []
    for lower in usable:
        for upper in usable:
            separation = float(upper["coordinate_hz"]) - float(
                lower["coordinate_hz"]
            )
            if not float(min_separation_hz) <= separation <= float(
                max_separation_hz
            ):
                continue
            if float(lower["fwhm_hz"]) > float(lower_max_width_ratio) * float(
                upper["fwhm_hz"]
            ):
                continue
            pair = {
                "lower_two_photon_candidate": dict(lower),
                "upper_fundamental_candidate": dict(upper),
                "separation_hz": separation,
                "width_ratio": float(lower["fwhm_hz"])
                / float(upper["fwhm_hz"]),
            }
            score = float(lower["robust_snr"]) + float(upper["robust_snr"])
            matches.append((score, pair))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def _feature_fwhm(coordinates: Any, values: Any, feature_index: int) -> float | None:
    """Measure a smoothed peak/dip FWHM around the dominant 03a feature."""
    import numpy as np

    x = np.asarray(coordinates, dtype=float).reshape(-1)
    y = np.asarray(values, dtype=float).reshape(-1)
    if x.size != y.size or x.size < 7 or not 0 <= feature_index < x.size:
        return None
    order = np.argsort(x)
    x = x[order]
    y = y[order]
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        return None
    edge_points = max(2, min(x.size // 10, 50))
    baseline = float(np.median(np.concatenate((y[:edge_points], y[-edge_points:]))))
    original_coordinate = float(coordinates[feature_index])
    sorted_index = int(np.argmin(np.abs(x - original_coordinate)))
    sign = 1.0 if y[sorted_index] >= baseline else -1.0
    profile = sign * (y - baseline)
    window = min(5, x.size if x.size % 2 else x.size - 1)
    if window >= 3:
        half_window = window // 2
        padded = np.pad(profile, (half_window, half_window), mode="edge")
        profile = np.convolve(padded, np.ones(window) / window, mode="valid")
    peak_index = int(np.argmax(profile))
    peak_height = float(profile[peak_index])
    if not math.isfinite(peak_height) or peak_height <= 0:
        return None
    half_height = peak_height / 2.0

    left = peak_index
    while left > 0 and profile[left] >= half_height:
        left -= 1
    right = peak_index
    while right < profile.size - 1 and profile[right] >= half_height:
        right += 1
    if (left == 0 and profile[left] >= half_height) or (
        right == profile.size - 1 and profile[right] >= half_height
    ):
        return None

    def crossing(i0: int, i1: int) -> float:
        y0 = float(profile[i0])
        y1 = float(profile[i1])
        if math.isclose(y0, y1):
            return float((x[i0] + x[i1]) / 2.0)
        fraction = (half_height - y0) / (y1 - y0)
        return float(x[i0] + fraction * (x[i1] - x[i0]))

    left_crossing = crossing(left, left + 1)
    right_crossing = crossing(right - 1, right)
    width = abs(right_crossing - left_crossing)
    return float(width) if math.isfinite(width) and width > 0 else None


def _fit_exponential_decay(times: Any, values: Any) -> dict[str, Any]:
    """Fit offset + amplitude * exp(-time / tau) without SciPy."""
    import numpy as np

    x = np.asarray(times, dtype=float).reshape(-1)
    y = np.asarray(values, dtype=float).reshape(-1)
    finite = np.isfinite(x) & np.isfinite(y)
    x = x[finite]
    y = y[finite]
    if x.size < 8:
        return {"error": "At least 8 finite T1 samples are required."}
    order = np.argsort(x)
    x = x[order]
    y = y[order]
    x = x - x[0]
    span = float(x[-1])
    positive_steps = np.diff(x)
    positive_steps = positive_steps[positive_steps > 0]
    if span <= 0 or positive_steps.size == 0:
        return {"error": "T1 sweep coordinates must increase."}
    dynamic_range = float(np.max(y) - np.min(y))
    if not math.isfinite(dynamic_range) or dynamic_range <= 0:
        return {"error": "T1 signal has no finite dynamic range."}

    median_step = float(np.median(positive_steps))
    lower = max(median_step / 4, span / 10000, np.finfo(float).eps)
    upper = max(span * 100, lower * 100)
    candidates = np.geomspace(lower, upper, 500)

    def evaluate(tau: float) -> tuple[float, float, float, Any]:
        exponential = np.exp(-x / tau)
        centered = exponential - np.mean(exponential)
        denominator = float(np.dot(centered, centered))
        if denominator <= np.finfo(float).eps:
            return math.inf, 0.0, float(np.mean(y)), exponential
        amplitude = float(np.dot(centered, y - np.mean(y)) / denominator)
        offset = float(np.mean(y) - amplitude * np.mean(exponential))
        residual = y - (offset + amplitude * exponential)
        return float(np.dot(residual, residual)), amplitude, offset, exponential

    evaluations = [evaluate(float(tau)) for tau in candidates]
    errors = np.asarray([item[0] for item in evaluations])
    best_index = int(np.argmin(errors))
    fit_at_boundary = best_index in {0, len(candidates) - 1}
    if not math.isfinite(float(errors[best_index])):
        return {"error": "T1 exponential fit did not converge."}

    left = candidates[max(best_index - 1, 0)]
    right = candidates[min(best_index + 1, len(candidates) - 1)]
    refined = np.geomspace(left, right, 100)
    refined_evaluations = [evaluate(float(tau)) for tau in refined]
    refined_errors = np.asarray([item[0] for item in refined_evaluations])
    refined_index = int(np.argmin(refined_errors))
    tau = float(refined[refined_index])
    sse, amplitude, offset, exponential = refined_evaluations[refined_index]

    total_variation = float(np.dot(y - np.mean(y), y - np.mean(y)))
    if total_variation <= np.finfo(float).eps:
        return {"error": "T1 signal variance is too small to fit."}
    r_squared = 1 - sse / total_variation
    derivative_tau = amplitude * exponential * x / (tau * tau)
    jacobian = np.column_stack((exponential, np.ones_like(x), derivative_tau))
    degrees_of_freedom = max(x.size - 3, 1)
    covariance = (sse / degrees_of_freedom) * np.linalg.pinv(
        jacobian.T @ jacobian
    )
    tau_variance = float(covariance[2, 2])
    tau_error = math.sqrt(max(tau_variance, 0.0))
    return {
        "tau": tau,
        "tau_error": tau_error,
        "relative_uncertainty": tau_error / tau,
        "r_squared": float(r_squared),
        "coverage_lifetimes": span / tau,
        "samples_per_lifetime": tau / median_step,
        "decay_amplitude": amplitude,
        "decay_offset": offset,
        "fit_at_search_boundary": fit_at_boundary,
    }


def _time_unit_scale(coordinate: Any) -> tuple[float, str]:
    raw = "" if coordinate is None else str(coordinate.attrs.get("units", ""))
    normalized = raw.replace("Â", "").replace("μ", "µ").strip().lower()
    if normalized in {"ns", "nanosecond", "nanoseconds"}:
        return 1e-9, "ns"
    if normalized in {"ms", "millisecond", "milliseconds"}:
        return 1e-3, "ms"
    if normalized in {"s", "second", "seconds"}:
        return 1.0, "s"
    if normalized in {
        "us",
        "usec",
        "µs",
        "µsec",
        "microsecond",
        "microseconds",
    } or "�s" in normalized:
        return 1e-6, "us"
    return 1e-6, "us (assumed by JY time-node contract)"


def _run_targets(
    run: dict[str, Any], data: dict[str, Any], outcomes: dict[str, Any]
) -> list[str]:
    raw = run.get("parameters_json")
    if isinstance(raw, str):
        try:
            parameters = json.loads(raw)
        except json.JSONDecodeError:
            parameters = {}
    else:
        parameters = raw if isinstance(raw, dict) else {}
    targets = parameters.get("qubits")
    if not isinstance(targets, list):
        targets = data.get("initial_parameters", {}).get("qubits")
    if not isinstance(targets, list):
        targets = list(outcomes)
    return [str(name) for name in targets]


#: The numbers the playbook actually gates on, per qubit. A digest keeps only
#: these, so a decision can be made from a handful of values instead of a whole
#: analysis document.
_DIGEST_METRIC_KEYS = (
    "robust_snr",
    "edge_fraction",
    "feature_fwhm_hz",
    "feature_coordinate",
    "morphology_pass",
)
_DIGEST_FIT_KEYS = (
    "fit_successful",
    "drive_freq",
    "Pi_amplitude",
    "t1_seconds",
    "coherence_seconds",
    "readout_fidelity",
    "morphology_pass",
    "active_reset_qualified",
    "relative_uncertainty",
    "r_squared",
    "coverage_lifetimes",
    "samples_per_lifetime",
    "fit_at_search_boundary",
    "EPC",
    "EPG",
    "recommended_statistics_max_wait_time_in_ns",
)


def evidence_digest(analysis: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce an analysis document to the values a decision is made from.

    Returns the run-level verdict plus, for each qubit, the gated metrics and
    fit values that are present. Nothing is recomputed: a digest is a view of
    the recorded analysis, never a second opinion about it.
    """

    if not isinstance(analysis, dict):
        return {}
    metrics_by_qubit = analysis.get("dataset_metrics", {})
    metrics_by_qubit = (
        metrics_by_qubit.get("qubits") if isinstance(metrics_by_qubit, dict) else None
    )
    fits_by_qubit = analysis.get("fit_quality", {})
    fits_by_qubit = (
        fits_by_qubit.get("results") if isinstance(fits_by_qubit, dict) else None
    )
    # Nodes without a per-qubit fitter record these as explicit nulls -- 02x
    # stores `"fit_quality": {"results": null}` -- so coerce anything that is
    # not a mapping before iterating it.
    if not isinstance(metrics_by_qubit, dict):
        metrics_by_qubit = {}
    if not isinstance(fits_by_qubit, dict):
        fits_by_qubit = {}
    names = sorted(
        {str(name) for name in metrics_by_qubit}
        | {str(name) for name in fits_by_qubit}
    )
    per_qubit: dict[str, dict[str, Any]] = {}
    for name in names:
        digest: dict[str, Any] = {}
        metrics = metrics_by_qubit.get(name) if isinstance(metrics_by_qubit, dict) else None
        if isinstance(metrics, dict):
            for key in _DIGEST_METRIC_KEYS:
                if key in metrics:
                    digest[key] = _json_safe(metrics[key])
        fit = fits_by_qubit.get(name) if isinstance(fits_by_qubit, dict) else None
        if isinstance(fit, dict):
            for key in _DIGEST_FIT_KEYS:
                if key in fit:
                    digest[key] = _json_safe(fit[key])
        if digest:
            per_qubit[name] = digest
    result: dict[str, Any] = {
        "analysis_status": analysis.get("analysis_status"),
        "node_id": analysis.get("node_id"),
        "qubits": per_qubit,
    }
    for key in (
        "passing_targets",
        "failure_reasons_by_target",
        "run_level_failure_reasons",
        "03a_stage",
    ):
        if key in analysis:
            result[key] = analysis[key]
    if analysis.get("analysis_status") == "failed":
        result["failure_reasons"] = analysis.get("failure_reasons", [])
        if analysis.get("failure_category"):
            result["failure_category"] = analysis["failure_category"]
    return result


def _attribute_failures(
    failures: list[str], targets: list[str]
) -> tuple[dict[str, list[str]], list[str]]:
    """Split failure reasons into per-target reasons and run-level reasons.

    Every per-qubit check in this module writes its reason as ``f"{name} ..."``.
    A reason that names no target belongs to the run as a whole -- a missing
    plot, an unreadable dataset, an invalid recorded update -- and must keep
    blocking every target.
    """

    by_target: dict[str, list[str]] = {}
    run_level: list[str] = []
    names = sorted({str(name) for name in targets}, key=len, reverse=True)
    for reason in failures:
        text = str(reason)
        for name in names:
            if text.startswith(f"{name} "):
                by_target.setdefault(name, []).append(text)
                break
        else:
            run_level.append(text)
    return by_target, run_level


def _reset_type(parameters: Any) -> str:
    if not isinstance(parameters, dict):
        return "thermal"
    value = parameters.get(
        "reset_type",
        parameters.get("reset_type_thermal_or_active", "thermal"),
    )
    return str(value).strip().casefold()


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        try:
            return _json_safe(value.tolist())
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _scalar_number(value: Any) -> float | None:
    safe = _json_safe(value)
    if _finite(safe):
        return float(safe)
    return None


def _resolve_snapshot_npz_references(snapshot: Path, value: Any) -> Any:
    """Resolve Qualibrate's local ``arrays.npz`` references without escaping a snapshot.

    Qualibrate stores some scalar fit values as strings such as
    ``./arrays.npz#fit_results.q3.decay``.  Keeping those strings unresolved
    makes an otherwise valid fit look missing.  Only NPZ files contained in the
    saved snapshot are eligible; all other references remain untouched.
    """

    reference = re.compile(r"^\./(?P<archive>[^#]+\.npz)#(?P<key>.+)$")
    root = snapshot.resolve()
    archives: dict[Path, dict[str, Any]] = {}

    def load(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: load(child) for key, child in item.items()}
        if isinstance(item, list):
            return [load(child) for child in item]
        if not isinstance(item, str):
            return item
        matched = reference.fullmatch(item)
        if matched is None:
            return item

        archive_path = (snapshot / matched.group("archive")).resolve()
        try:
            archive_path.relative_to(root)
        except ValueError as exc:
            raise AnalysisError(
                f"Snapshot array reference escapes the snapshot: {item}"
            ) from exc
        if not archive_path.is_file():
            raise AnalysisError(f"Snapshot array archive is missing: {item}")

        if archive_path not in archives:
            import numpy as np

            with np.load(archive_path, allow_pickle=False) as archive:
                archives[archive_path] = {
                    key: _json_safe(archive[key]) for key in archive.files
                }
        key = matched.group("key")
        if key not in archives[archive_path]:
            raise AnalysisError(f"Snapshot array key is missing: {item}")
        return archives[archive_path][key]

    return load(value)
