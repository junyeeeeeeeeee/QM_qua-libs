from __future__ import annotations

import math
from typing import Any, Iterable

from .util import json_loads
from .workflow_02c import resolve_02c_targets


SUBGROUP_NODES = frozenset(
    {
        "02c",
        "03a",
        "04",
        "05",
        "07b",
        "06",
        "06b",
        "10a",
        "05st",
        "06st_t2star",
        "06st_t2e",
    }
)
# These calibration nodes share one fixed multiplex sweep definition.  A retry
# may widen/refine the sweep, but it must not silently turn into a target-local
# run because the calibration script and evidence are interpreted as one batch.
FIXED_TARGET_NODES = frozenset({"02x", "02a"})
DEFAULT_03A_MIN_ROBUST_SNR = 10.0
DEFAULT_03A_MIN_EDGE_FRACTION = 0.05
DEFAULT_03A_MAX_NOISE_CONFIRMATION_AVERAGES = 2000
DEFAULT_03A_FINAL_MIN_FWHM_HZ = 500_000.0
DEFAULT_03A_FINAL_MAX_FWHM_HZ = 10_000_000.0
DEFAULT_04_MIN_ROBUST_SNR = 14.0
DEFAULT_05_MIN_ROBUST_SNR = 25.0


def resolve_node_targets(
    node_id: str,
    run_rows: Iterable[dict[str, Any]],
    min_03a_snr: float = DEFAULT_03A_MIN_ROBUST_SNR,
    min_04_snr: float = DEFAULT_04_MIN_ROBUST_SNR,
    min_05_snr: float = DEFAULT_05_MIN_ROBUST_SNR,
) -> set[str]:
    """Return targets with final per-qubit evidence for one node."""
    rows = list(run_rows)
    if node_id == "02c":
        resolved, _ = resolve_02c_targets(rows)
        return resolved
    if node_id not in SUBGROUP_NODES - {"02c"}:
        return set()
    if node_id == "03a":
        return _resolve_03a_targets(
            rows,
            require_fine=True,
            min_snr=min_03a_snr,
        )
    return _resolve_usable_targets(
        node_id,
        rows,
        require_fine_03a=False,
        min_03a_snr=min_03a_snr,
        min_04_snr=min_04_snr,
        min_05_snr=min_05_snr,
    )


def resolve_03a_candidate_targets(
    run_rows: Iterable[dict[str, Any]],
    min_snr: float = DEFAULT_03A_MIN_ROBUST_SNR,
) -> set[str]:
    """Return credible 03a candidates after explicit human rejections."""
    return _resolve_03a_targets(
        list(run_rows), require_fine=False, min_snr=min_snr
    )


def _resolve_03a_targets(
    rows: list[dict[str, Any]],
    *,
    require_fine: bool,
    min_snr: float,
) -> set[str]:
    resolved: set[str] = set()
    for row in rows:
        resolved.update(
            _resolve_usable_targets(
                "03a",
                [row],
                require_fine_03a=require_fine,
                min_03a_snr=min_snr,
            )
        )
        if row.get("decision") == "manual_review":
            parameters = json_loads(row.get("parameters_json"), {})
            names = parameters.get("qubits", [])
            if isinstance(names, list):
                resolved.difference_update(str(name) for name in names)
    return resolved


def resolve_03a_shift_ready_targets(
    run_rows: Iterable[dict[str, Any]],
    *,
    min_snr: float = DEFAULT_03A_MIN_ROBUST_SNR,
    min_edge_fraction: float = DEFAULT_03A_MIN_EDGE_FRACTION,
    max_noise_confirmation_averages: int = (
        DEFAULT_03A_MAX_NOISE_CONFIRMATION_AVERAGES
    ),
) -> set[str]:
    """Return unresolved targets allowed to move to another LO window.

    Edge-limited features may shift immediately. Interior low-SNR traces must
    first be reconfirmed with at least the configured maximum averages.
    """
    rows = list(run_rows)
    candidates = resolve_03a_candidate_targets(rows, min_snr)
    latest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for row in rows:
        if row.get("status") not in {None, "completed"}:
            continue
        if row.get("analysis_status") not in {"pass", "needs_review", "failed"}:
            continue
        analysis = json_loads(row.get("analysis_json"), {})
        parameters = json_loads(row.get("parameters_json"), {})
        metrics_by_name = analysis.get("dataset_metrics", {}).get("qubits", {})
        names = parameters.get("qubits", [])
        if not isinstance(names, list):
            names = []
        for name in names:
            metrics = (
                metrics_by_name.get(name, {})
                if isinstance(metrics_by_name, dict)
                else {}
            )
            latest[str(name)] = (parameters, metrics)

    ready: set[str] = set()
    for name, (parameters, metrics) in latest.items():
        if name in candidates:
            continue
        edge = metrics.get("edge_fraction") if isinstance(metrics, dict) else None
        averages = parameters.get("num_averages")
        edge_limited = _finite(edge) and float(edge) < float(min_edge_fraction)
        sufficiently_averaged = (
            isinstance(averages, int)
            and not isinstance(averages, bool)
            and averages >= int(max_noise_confirmation_averages)
        )
        if edge_limited or sufficiently_averaged:
            ready.add(name)
    return ready


def _resolve_usable_targets(
    node_id: str,
    rows: list[dict[str, Any]],
    *,
    require_fine_03a: bool,
    min_03a_snr: float,
    min_04_snr: float = DEFAULT_04_MIN_ROBUST_SNR,
    min_05_snr: float = DEFAULT_05_MIN_ROBUST_SNR,
) -> set[str]:
    resolved: set[str] = set()
    for row in rows:
        if row.get("status") not in {None, "completed"}:
            continue
        if row.get("analysis_status") not in {"pass", "needs_review"}:
            continue
        analysis = json_loads(row.get("analysis_json"), {})
        if (
            node_id == "03a"
            and require_fine_03a
            and analysis.get("03a_stage") != "fine"
        ):
            continue
        results = analysis.get("fit_quality", {}).get("results", {})
        metrics = analysis.get("dataset_metrics", {}).get("qubits", {})
        if not isinstance(results, dict) or not isinstance(metrics, dict):
            continue
        for name, fit in results.items():
            if not isinstance(fit, dict):
                continue
            if not _fit_is_usable(node_id, fit):
                continue
            if node_id in {
                "07b",
                "06",
                "06b",
                "10a",
                "05st",
                "06st_t2star",
                "06st_t2e",
            }:
                resolved.add(str(name))
                continue
            evidence = metrics.get(name)
            if node_id == "05":
                if _snr_evidence_is_usable(evidence, min_05_snr):
                    resolved.add(str(name))
                continue
            if node_id == "03a" and require_fine_03a:
                width = (
                    evidence.get("feature_fwhm_hz")
                    if isinstance(evidence, dict)
                    else None
                )
                if not _finite(width) or not (
                    DEFAULT_03A_FINAL_MIN_FWHM_HZ
                    <= float(width)
                    <= DEFAULT_03A_FINAL_MAX_FWHM_HZ
                ):
                    continue
            min_snr = {
                "03a": min_03a_snr,
                "04": min_04_snr,
                "05": min_05_snr,
            }.get(node_id, 5.0)
            if not _sweep_evidence_is_usable(evidence, min_snr):
                continue
            resolved.add(str(name))
    return resolved


def _fit_is_usable(node_id: str, fit: dict[str, Any]) -> bool:
    if node_id == "03a":
        return fit.get("fit_successful") is True and _finite(
            fit.get("drive_freq")
        )
    if node_id == "04":
        amplitude = fit.get("Pi_amplitude")
        return _finite(amplitude) and float(amplitude) != 0.0
    if node_id == "05":
        t1 = fit.get("t1_seconds")
        relative_uncertainty = fit.get("relative_uncertainty")
        r_squared = fit.get("r_squared")
        coverage = fit.get("coverage_lifetimes")
        samples_per_lifetime = fit.get("samples_per_lifetime")
        return (
            _finite(t1)
            and float(t1) > 0
            and _finite(relative_uncertainty)
            and 0 <= float(relative_uncertainty) < 0.25
            and _finite(r_squared)
            and float(r_squared) >= 0.9
            and _finite(coverage)
            and float(coverage) >= 3.5
            and _finite(samples_per_lifetime)
            and float(samples_per_lifetime) >= 2
            and fit.get("fit_at_search_boundary") is not True
        )
    if node_id == "07b":
        fidelity = fit.get("readout_fidelity")
        return (
            fit.get("fit_successful") is True
            and _finite(fidelity)
            and 0.5 <= float(fidelity) <= 1
        )
    if node_id in {"06", "06b"}:
        lifetime = fit.get("coherence_seconds")
        relative_uncertainty = fit.get("relative_uncertainty")
        coverage = fit.get("coverage_lifetimes")
        samples = fit.get("samples_per_lifetime")
        return (
            fit.get("fit_successful") is True
            and _finite(lifetime)
            and float(lifetime) > 0
            and _finite(relative_uncertainty)
            and 0 <= float(relative_uncertainty) < 0.25
            and _finite(coverage)
            and float(coverage) >= 3.5
            and _finite(samples)
            and float(samples) >= 2
            and (
                node_id != "06b"
                or (
                    _finite(fit.get("r_squared"))
                    and float(fit["r_squared"]) >= 0.9
                    and fit.get("fit_at_search_boundary") is not True
                )
            )
        )
    if node_id == "10a":
        epc = fit.get("EPC")
        epg = fit.get("EPG")
        return (
            fit.get("fit_successful") is True
            and _finite(epc)
            and 0 <= float(epc) <= 1
            and _finite(epg)
            and 0 <= float(epg) <= 1
        )
    if node_id in {"05st", "06st_t2star", "06st_t2e"}:
        return (
            fit.get("fit_successful") is True
            and fit.get("histo_num") == 100
            and fit.get("raw_iteration_refit_required") is False
        )
    return False


def _sweep_evidence_is_usable(metrics: Any, min_snr: float) -> bool:
    if not isinstance(metrics, dict) or metrics.get("error"):
        return False
    edge = metrics.get("edge_fraction")
    snr = metrics.get("robust_snr")
    return (
        _finite(edge)
        and float(edge) >= DEFAULT_03A_MIN_EDGE_FRACTION
        and _finite(snr)
        and float(snr) >= float(min_snr)
    )


def _snr_evidence_is_usable(metrics: Any, min_snr: float) -> bool:
    if not isinstance(metrics, dict) or metrics.get("error"):
        return False
    snr = metrics.get("robust_snr")
    return _finite(snr) and float(snr) >= float(min_snr)


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
