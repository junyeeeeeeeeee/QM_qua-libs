# %% {Imports}
from qualibrate import QualibrationNode, NodeParameters
from quam_libs.components import QuAM
from quam_libs.macros import active_reset, readout_state, readout_state_gef
from quam_libs.lib.save_utils import (
    fetch_results_as_xarray,
    restore_load_data_id,
    resolve_qubit_pairs_from_node,
)
from qualang_tools.results import progress_counter, fetching_tool
from qualang_tools.multi_user import qm_session
from qualang_tools.units import unit
from qm import SimulationConfig
from qm.qua import *
from typing import Literal, Optional, List, Dict
import matplotlib.pyplot as plt
import numpy as np
from quam_libs.lib.fit import oscillation
from quam_libs.lib.plot_utils import QubitPairGrid, grid_iter, grid_pair_names
from calibration_utils.flux_landscape_fitting import (
    fit_coupler_zeropoint_pair,
    fit_coupler_zeropoint_to_legacy_results,
)
from calibration_utils.flux_landscape_fitting.analysis import FluxLandscapeFit
from calibration_utils.flux_landscape_fitting.plotting import _add_detuning_axis

# %% {Description}
description = """
        QUBIT FLUX × GATE DURATION ON RESONANCE

Variant of coupler zero-interaction calibration where the 2D sweep is:
    - Qubit control flux (bring qubits near resonance)  → plot x-axis
    - Gate / flux-pulse duration                         → plot y-axis

Coupler flux is held fixed at the selected gate coupler amplitude.
The same contrast-cut fitting pipeline as 61x is reused (duration occupies
the former coupler-flux dataset axis for analysis compatibility).
"""

# %% {Node_parameters}
qubit_pair_indexes = [3]  # [1, 2]


class Parameters(NodeParameters):
    qubit_pairs: Optional[List[str]] = ["coupler_q%s_q%s" % (i, i + 1) for i in qubit_pair_indexes]
    num_averages: int = 100
    flux_point_joint_or_independent_or_pairwise: Literal["joint", "independent", "pairwise"] = "joint"
    reset_type: Literal["active", "thermal"] = "active"
    simulate: bool = False
    timeout: int = 200
    load_data_id: Optional[int] = None

    qubit_flux_span: float = 0.03  # relative to the selected gate qubit flux
    qubit_flux_step: float = 0.0003
    guess_flux_detuning: float | None = None
    use_state_discrimination: bool = True

    duration_min_ns: int = 16
    duration_max_ns: int = 200
    duration_step_ns: int = 4

    cz_or_iswap: Literal["cz", "iswap"] = "cz"
    operation: Literal["Cz_flattop", "Cz_unipolar", "Cz_bipolar", "Cz"] = "Cz"
    """CZ gate variant to calibrate. Ignored when cz_or_iswap is 'iswap'."""
    use_saved_detuning: bool = True
    con_tar_flip: bool = False

    analysis_fit_preset: Literal["default", "noisy", "coarse"] = "default"
    """Contrast-cut fit preset (Savitzky–Golay + sliding-window FFT)."""
    analysis_debug: bool = True
    """If True, also plot the 1D contrast-cut diagnostic figure."""


node = QualibrationNode(name="61x2_qubit_flux_duration_on_resonance", parameters=Parameters())
assert not (
    node.parameters.simulate and node.parameters.load_data_id is not None
), "If simulate is True, load_data_id must be None, and vice versa."

# %% {Initialize_QuAM_and_QOP}
u = unit(coerce_to_integer=True)
machine = QuAM.load()
node.machine = machine

if node.parameters.qubit_pairs is None or node.parameters.qubit_pairs == "":
    qubit_pairs = machine.active_qubit_pairs
else:
    qubit_pairs = [machine.qubit_pairs[qp] for qp in node.parameters.qubit_pairs]

num_qubit_pairs = len(qubit_pairs)

config = machine.generate_config()
octave_config = machine.get_octave_config()
if node.parameters.load_data_id is None:
    qmm = machine.connect()


# %%
####################
# Helper functions #
####################


def resolve_operation(qp):
    """Return (operation_name, coupler_pulse_attr) for the configured gate variant."""
    if node.parameters.cz_or_iswap == "iswap":
        return "SWAP", "coupler_pulse_control"
    return node.parameters.operation, "coupler_flux_pulse"


def qubit_flux_center(qp, operation_name):
    """Qubit flux sweep center from the selected gate, with fallbacks for first calibration."""
    gate_amp = qp.gates[operation_name].flux_pulse_control.amplitude
    if gate_amp is not None:
        return gate_amp
    if node.parameters.guess_flux_detuning is not None:
        return node.parameters.guess_flux_detuning
    if node.parameters.use_saved_detuning and qp.detuning is not None:
        return qp.detuning
    if node.parameters.cz_or_iswap == "iswap":
        return np.sqrt(
            -(qp.qubit_control.xy.RF_frequency - qp.qubit_target.xy.RF_frequency)
            / qp.qubit_control.freq_vs_flux_01_quad_term
        )
    return np.sqrt(
        -(qp.qubit_control.xy.RF_frequency - qp.qubit_target.xy.RF_frequency - qp.qubit_target.anharmonicity)
        / qp.qubit_control.freq_vs_flux_01_quad_term
    )


def plot_qubit_flux_duration_maps(
    ds,
    qubit_pairs,
    results: Dict[str, dict],
    *,
    use_state_discrimination: bool,
    fits: Optional[Dict[str, FluxLandscapeFit]] = None,
    analysis_debug: bool = False,
):
    """Same layout as plot_coupler_zeropoint_maps, but y-axis is gate duration [ns]."""
    grid_names, qubit_pair_names = grid_pair_names(qubit_pairs)
    figures: Dict[str, plt.Figure] = {}

    for state_type in ["control", "target"]:
        grid = QubitPairGrid(grid_names, qubit_pair_names)
        for ax, qp in grid_iter(grid):
            qubit_name = qp["qubit"]
            try:
                if use_state_discrimination:
                    values_to_plot = ds[f"state_{state_type}"].sel(qubit=qubit_name)
                else:
                    values_to_plot = ds[f"I_{state_type}"].sel(qubit=qubit_name)
                values_to_plot = values_to_plot.assign_coords(
                    {
                        "flux_qubit_mV": 1e3 * values_to_plot.flux_qubit_full,
                        "duration_ns": values_to_plot.flux_coupler_full,
                    }
                )
                values_to_plot.plot(ax=ax, cmap="viridis", x="flux_qubit_mV", y="duration_ns")
            except Exception as e:
                print(f"[WARN] Plot data failed for {qubit_name}: {e}")
                ax.set_title(f"{qubit_name} (raw plot failed)")
                continue

            res = results.get(qubit_name, {})
            has_marker = False
            if np.isfinite(res.get("flux_coupler_min_full", np.nan)):
                ax.axhline(res["flux_coupler_min_full"], color="red", lw=2.0, ls="--", label="Flat / idle duration")
                has_marker = True
            if np.isfinite(res.get("flux_coupler_max_full", np.nan)):
                ax.axhline(res["flux_coupler_max_full"], color="black", lw=1.0, ls=":")
                has_marker = True
            if np.isfinite(res.get("flux_qubit_max", np.nan)):
                ax.axvline(1e3 * res["flux_qubit_max"], color="black", lw=1.0, ls=":")
                has_marker = True
            if np.isfinite(res.get("flux_qubit_max", np.nan)) and np.isfinite(res.get("flux_coupler_max_full", np.nan)):
                ax.plot(
                    1e3 * res["flux_qubit_max"],
                    res["flux_coupler_max_full"],
                    marker="+",
                    color="black",
                    markersize=10,
                    mew=2.0,
                    label="Gate starting point",
                )
            elif not has_marker and res.get("fit_success") is False:
                ax.text(0.02, 0.98, "fit failed", transform=ax.transAxes, va="top", ha="left", fontsize=8, color="red")
            if has_marker:
                ax.legend(fontsize=7, loc="upper right", frameon=True)

            _add_detuning_axis(ax, ds, qubit_name, "flux_qubit_full")
            ax.set_xlabel("Qubit flux shift [mV]")
            ax.set_ylabel("Gate duration [ns]")
            ax.set_title(f"{qubit_name}", fontsize=9)

        grid.fig.suptitle(f"{state_type.capitalize()} Qubit", y=0.97, fontsize=12, weight="bold")
        plt.tight_layout()
        figures[f"figure_{state_type}"] = grid.fig

    if analysis_debug and fits:
        figures["contrast_debug"] = _plot_duration_contrast_cut_debug(fits, qubit_pairs)
    return figures


def _plot_duration_contrast_cut_debug(
    fits: Dict[str, FluxLandscapeFit],
    qubit_pairs: list,
    *,
    ylabel: str = "|contrast| (|control − target|)",
):
    """1D contrast-cut debug with duration [ns] on the x-axis."""
    grid_names, qubit_pair_names = grid_pair_names(qubit_pairs)
    grid = QubitPairGrid(grid_names, qubit_pair_names)

    for ax, qp in grid_iter(grid):
        qp_name = qp["qubit"]
        fit = fits.get(qp_name)
        if fit is None or fit.contrast_raw is None:
            ax.set_title(f"{qp_name} (no cut data)")
            continue

        x_v = fit.contrast_coupler_full if fit.contrast_coupler_full is not None else fit.contrast_coupler_rel
        x = np.asarray(x_v).ravel()
        y = np.asarray(fit.contrast_raw).ravel()
        smoothed = np.asarray(fit.contrast_smoothed).ravel()
        osc_mask = np.asarray(fit.osc_mask).astype(bool)
        flat_mask = np.asarray(fit.flat_mask).astype(bool)

        ax.plot(x, y, color="steelblue", lw=1.0, alpha=0.4, label="raw")
        ax.plot(x, smoothed, color="steelblue", lw=1.8, label="smoothed")
        ax.fill_between(
            x, 0, 1, where=osc_mask, alpha=0.12, color="limegreen", transform=ax.get_xaxis_transform(), label="oscillation"
        )
        ax.fill_between(
            x, 0, 1, where=flat_mask, alpha=0.15, color="tomato", transform=ax.get_xaxis_transform(), label="flat"
        )
        ax.axhline(0, color="gray", ls=":", lw=0.8)

        if np.isfinite(fit.optimal_decouple_offset):
            ax.axvline(fit.optimal_decouple_offset, color="red", ls="--", lw=1.5, label="Flat duration")
        if np.isfinite(fit.optimal_gate_coupler_flux_total):
            ax.axvline(fit.optimal_gate_coupler_flux_total, color="green", ls="--", lw=1.5, label="Gate duration")
        if np.isfinite(fit.optimal_qubit_flux):
            ax.set_title(f"{qp_name} @ qubit flux {fit.optimal_qubit_flux * 1e3:.1f} mV")
        else:
            ax.set_title(qp_name)

        ax.set_xlabel("Gate duration [ns]")
        ax.set_ylabel(ylabel)
        ax.legend(fontsize=6, loc="upper left")

    grid.fig.tight_layout()
    return grid.fig


# %% {QUA_program}
n_avg = node.parameters.num_averages
flux_point = node.parameters.flux_point_joint_or_independent_or_pairwise

fluxes_qubit = np.arange(
    -node.parameters.qubit_flux_span / 2,
    node.parameters.qubit_flux_span / 2 + 0.0001,
    node.parameters.qubit_flux_step,
)

assert node.parameters.duration_min_ns % 4 == 0, "duration_min_ns must be divisible by 4"
assert node.parameters.duration_max_ns % 4 == 0, "duration_max_ns must be divisible by 4"
assert node.parameters.duration_step_ns % 4 == 0, "duration_step_ns must be divisible by 4"
assert node.parameters.duration_min_ns >= 16, "duration_min_ns must be >= 16 ns"

# Absolute durations [ns]; reused as the analysis "flux_coupler" axis.
durations_ns = np.arange(
    node.parameters.duration_min_ns,
    node.parameters.duration_max_ns + 0.1,
    node.parameters.duration_step_ns,
).astype(int)
durations_cc = (durations_ns // 4).astype(int)

fluxes_qp = {}
coupler_amps_qp = {}
for qp in qubit_pairs:
    operation_name, coupler_attr = resolve_operation(qp)
    gate = qp.gates[operation_name]
    qubit_center = qubit_flux_center(qp, operation_name)
    coupler_amps_qp[qp.name] = getattr(gate, coupler_attr).amplitude
    fluxes_qp[qp.name] = fluxes_qubit + qubit_center

reset_coupler_bias = False

with program() as qubit_flux_duration_on_resonance:
    n = declare(int)
    flux_qubit = declare(float)
    comp_flux_qubit = declare(float)
    qua_pulse_duration = declare(int)
    n_st = declare_stream()
    if node.parameters.use_state_discrimination:
        state_control = [declare(int) for _ in range(num_qubit_pairs)]
        state_target = [declare(int) for _ in range(num_qubit_pairs)]
        state = [declare(int) for _ in range(num_qubit_pairs)]
        state_st_control = [declare_stream() for _ in range(num_qubit_pairs)]
        state_st_target = [declare_stream() for _ in range(num_qubit_pairs)]
        state_st = [declare_stream() for _ in range(num_qubit_pairs)]
    else:
        I_control = [declare(float) for _ in range(num_qubit_pairs)]
        Q_control = [declare(float) for _ in range(num_qubit_pairs)]
        I_target = [declare(float) for _ in range(num_qubit_pairs)]
        Q_target = [declare(float) for _ in range(num_qubit_pairs)]
        I_st_control = [declare_stream() for _ in range(num_qubit_pairs)]
        Q_st_control = [declare_stream() for _ in range(num_qubit_pairs)]
        I_st_target = [declare_stream() for _ in range(num_qubit_pairs)]
        Q_st_target = [declare_stream() for _ in range(num_qubit_pairs)]

    for i, qp in enumerate(qubit_pairs):
        print("qubit control: %s, qubit target: %s" % (qp.qubit_control.name, qp.qubit_target.name))
        machine.set_all_fluxes(flux_point, qp)
        if reset_coupler_bias:
            qp.coupler.set_dc_offset(0.0)
        else:
            qp.coupler.to_decouple_idle()
        wait(1000)

        coupler_amp = coupler_amps_qp[qp.name]

        with for_(n, 0, n < n_avg, n + 1):
            save(n, n_st)
            with for_each_(qua_pulse_duration, durations_cc):
                with for_each_(flux_qubit, fluxes_qp[qp.name]):
                    if not node.parameters.simulate:
                        if node.parameters.reset_type == "active":
                            active_reset(qp.qubit_control)
                            active_reset(qp.qubit_target)
                            qp.align()
                        else:
                            wait(qp.qubit_control.thermalization_time * u.ns)
                            wait(qp.qubit_target.thermalization_time * u.ns)
                    align()
                    if "coupler_qubit_crosstalk" in qp.extras:
                        assign(
                            comp_flux_qubit,
                            flux_qubit + qp.extras["coupler_qubit_crosstalk"] * coupler_amp,
                        )
                    else:
                        assign(comp_flux_qubit, flux_qubit)
                    qp.qubit_control.xy.play("x180")
                    if node.parameters.cz_or_iswap == "cz":
                        qp.qubit_target.xy.play("x180")
                    align()
                    qp.qubit_control.z.play(
                        "const",
                        amplitude_scale=comp_flux_qubit / qp.qubit_control.z.operations["const"].amplitude,
                        duration=qua_pulse_duration,
                    )
                    qp.coupler.play(
                        "const",
                        amplitude_scale=coupler_amp / qp.coupler.operations["const"].amplitude,
                        duration=qua_pulse_duration,
                    )
                    align()
                    wait(20)
                    if node.parameters.use_state_discrimination:
                        if node.parameters.cz_or_iswap == "cz":
                            if not node.parameters.con_tar_flip:
                                readout_state_gef(qp.qubit_control, state_control[i])
                                readout_state(qp.qubit_target, state_target[i])
                            else:
                                readout_state(qp.qubit_control, state_control[i])
                                readout_state_gef(qp.qubit_target, state_target[i])
                        else:
                            readout_state(qp.qubit_control, state_control[i])
                            readout_state(qp.qubit_target, state_target[i])
                        assign(state[i], state_control[i] * 2 + state_target[i])
                        save(state_control[i], state_st_control[i])
                        save(state_target[i], state_st_target[i])
                        save(state[i], state_st[i])
                    else:
                        qp.qubit_control.resonator.measure("readout", qua_vars=(I_control[i], Q_control[i]))
                        qp.qubit_target.resonator.measure("readout", qua_vars=(I_target[i], Q_target[i]))
                        save(I_control[i], I_st_control[i])
                        save(Q_control[i], Q_st_control[i])
                        save(I_target[i], I_st_target[i])
                        save(Q_target[i], Q_st_target[i])
        align()

    with stream_processing():
        n_st.save("n")
        for i in range(num_qubit_pairs):
            if node.parameters.use_state_discrimination:
                state_st_control[i].buffer(len(fluxes_qubit)).buffer(len(durations_ns)).average().save(
                    f"state_control{i + 1}"
                )
                state_st_target[i].buffer(len(fluxes_qubit)).buffer(len(durations_ns)).average().save(
                    f"state_target{i + 1}"
                )
                state_st[i].buffer(len(fluxes_qubit)).buffer(len(durations_ns)).average().save(f"state{i + 1}")
            else:
                I_st_control[i].buffer(len(fluxes_qubit)).buffer(len(durations_ns)).average().save(
                    f"I_control{i + 1}"
                )
                Q_st_control[i].buffer(len(fluxes_qubit)).buffer(len(durations_ns)).average().save(
                    f"Q_control{i + 1}"
                )
                I_st_target[i].buffer(len(fluxes_qubit)).buffer(len(durations_ns)).average().save(
                    f"I_target{i + 1}"
                )
                Q_st_target[i].buffer(len(fluxes_qubit)).buffer(len(durations_ns)).average().save(
                    f"Q_target{i + 1}"
                )

# %% {Simulate_or_execute}
if node.parameters.simulate:
    simulation_config = SimulationConfig(duration=10_000)
    job = qmm.simulate(config, qubit_flux_duration_on_resonance, simulation_config)
    job.get_simulated_samples().con1.plot()
    node.results = {"figure": plt.gcf()}
    node.save()
elif node.parameters.load_data_id is None:
    with qm_session(qmm, config, timeout=node.parameters.timeout) as qm:
        from qm import generate_qua_script

        with open("debug.py", "w+") as f:
            f.write(generate_qua_script(qubit_flux_duration_on_resonance, config))
        job = qm.execute(qubit_flux_duration_on_resonance)

        results = fetching_tool(job, ["n"], mode="live")
        while results.is_processing():
            n = results.fetch_all()[0]
            progress_counter(n, n_avg, start_time=results.start_time)

# %% {Data_fetching_and_dataset_creation}
if not node.parameters.simulate:
    if node.parameters.load_data_id is None:
        # Keep dim name "flux_coupler" so fit_coupler_zeropoint_pair can be reused unchanged.
        ds = fetch_results_as_xarray(
            job.result_handles,
            qubit_pairs,
            {"flux_qubit": fluxes_qubit, "flux_coupler": durations_ns.astype(float)},
        )
        flux_qubit_full = np.array([fluxes_qp[qp.name] for qp in qubit_pairs])
        duration_full = np.array([durations_ns.astype(float) for _ in qubit_pairs])
        ds = ds.assign_coords({"flux_qubit_full": (["qubit", "flux_qubit"], flux_qubit_full)})
        ds = ds.assign_coords({"flux_coupler_full": (["qubit", "flux_coupler"], duration_full)})
    else:
        load_data_id = node.parameters.load_data_id
        node = node.load_from_id(load_data_id)
        ds = node.results["ds"]
        restore_load_data_id(node, load_data_id)
        machine = node.machine
        qubit_pairs = resolve_qubit_pairs_from_node(machine, node)
        coupler_amps_qp = {}
        for qp in qubit_pairs:
            operation_name, coupler_attr = resolve_operation(qp)
            coupler_amps_qp[qp.name] = getattr(qp.gates[operation_name], coupler_attr).amplitude

    node.results = {"ds": ds}

# %% Data processing
detuning_mode = "quadratic"
if not node.parameters.simulate:
    if node.parameters.load_data_id is None:
        if detuning_mode == "quadratic":
            detuning = np.array(
                [-fluxes_qp[qp.name] ** 2 * qp.qubit_control.freq_vs_flux_01_quad_term for qp in qubit_pairs]
            )
        elif detuning_mode == "cosine":
            detuning = np.array(
                [
                    oscillation(
                        fluxes_qubit,
                        qp.qubit_control.extras["a"],
                        qp.qubit_control.extras["f"],
                        qp.qubit_control.extras["phi"],
                        qp.qubit_control.extras["offset"],
                    )
                    for qp in qubit_pairs
                ]
            )
        ds = ds.assign_coords({"detuning": (["qubit", "flux_qubit"], detuning)})
    elif (
        "flux_qubit_full" not in ds.coords
        or "flux_coupler_full" not in ds.coords
        or "detuning" not in ds.coords
    ):
        fluxes_qubit = np.arange(
            -node.parameters.qubit_flux_span / 2,
            node.parameters.qubit_flux_span / 2 + 0.0001,
            node.parameters.qubit_flux_step,
        )
        durations_ns = np.arange(
            node.parameters.duration_min_ns,
            node.parameters.duration_max_ns + 0.1,
            node.parameters.duration_step_ns,
        ).astype(int)
        fluxes_qp = {}
        for qp in qubit_pairs:
            operation_name, coupler_attr = resolve_operation(qp)
            fluxes_qp[qp.name] = fluxes_qubit + qubit_flux_center(qp, operation_name)
        flux_qubit_full = np.array([fluxes_qp[qp.name] for qp in qubit_pairs])
        duration_full = np.array([durations_ns.astype(float) for _ in qubit_pairs])
        ds = ds.assign_coords({"flux_qubit_full": (["qubit", "flux_qubit"], flux_qubit_full)})
        ds = ds.assign_coords({"flux_coupler_full": (["qubit", "flux_coupler"], duration_full)})
        if detuning_mode == "quadratic":
            detuning = np.array(
                [-fluxes_qp[qp.name] ** 2 * qp.qubit_control.freq_vs_flux_01_quad_term for qp in qubit_pairs]
            )
        elif detuning_mode == "cosine":
            detuning = np.array(
                [
                    oscillation(
                        fluxes_qubit,
                        qp.qubit_control.extras["a"],
                        qp.qubit_control.extras["f"],
                        qp.qubit_control.extras["phi"],
                        qp.qubit_control.extras["offset"],
                    )
                    for qp in qubit_pairs
                ]
            )
        ds = ds.assign_coords({"detuning": (["qubit", "flux_qubit"], detuning)})
    node.results = {"ds": ds}

if not node.parameters.simulate and (
    "flux_qubit_full" not in ds.coords or "flux_coupler_full" not in ds.coords
):
    if "fluxes_qp" not in globals():
        _fluxes_qubit = np.arange(
            -node.parameters.qubit_flux_span / 2,
            node.parameters.qubit_flux_span / 2 + 0.0001,
            node.parameters.qubit_flux_step,
        )
        _durations_ns = np.arange(
            node.parameters.duration_min_ns,
            node.parameters.duration_max_ns + 0.1,
            node.parameters.duration_step_ns,
        ).astype(int)
        fluxes_qp = {}
        for qp in qubit_pairs:
            operation_name, coupler_attr = resolve_operation(qp)
            fluxes_qp[qp.name] = _fluxes_qubit + qubit_flux_center(qp, operation_name)
        durations_ns = _durations_ns
    flux_qubit_full = np.array([fluxes_qp[qp.name] for qp in qubit_pairs])
    duration_full = np.array([durations_ns.astype(float) for _ in qubit_pairs])
    ds = ds.assign_coords({"flux_qubit_full": (["qubit", "flux_qubit"], flux_qubit_full)})
    ds = ds.assign_coords({"flux_coupler_full": (["qubit", "flux_coupler"], duration_full)})
    node.results["ds"] = ds

# %% Data Analysis
if not node.parameters.simulate:
    if "coupler_amps_qp" not in globals():
        coupler_amps_qp = {}
        for qp in qubit_pairs:
            operation_name, coupler_attr = resolve_operation(qp)
            coupler_amps_qp[qp.name] = getattr(qp.gates[operation_name], coupler_attr).amplitude

    node.results["results"] = {}
    flux_fits_qp = {}
    for qp in qubit_pairs:
        try:
            fit = fit_coupler_zeropoint_pair(
                ds,
                qp.name,
                use_state_discrimination=node.parameters.use_state_discrimination,
                cz_or_iswap=node.parameters.cz_or_iswap,
                preset=node.parameters.analysis_fit_preset,
            )
            flux_fits_qp[qp.name] = fit
            # decouple_offset=0: duration axis values must not be shifted by a flux bias.
            node.results["results"][qp.name] = fit_coupler_zeropoint_to_legacy_results(
                fit,
                decouple_offset=0.0,
                coupler_center=coupler_amps_qp.get(qp.name),
            )
            res = node.results["results"][qp.name]

            def _mv(v):
                return f"{v * 1e3:.1f}" if np.isfinite(v) else "NaN"

            def _ns(v):
                return f"{v:.0f}" if np.isfinite(v) else "NaN"

            print(
                f"{qp.name}: qubit={_mv(res['flux_qubit_max'])} mV, "
                f"duration_gate={_ns(res['flux_coupler_max_full'])} ns, "
                f"duration_flat={_ns(res['flux_coupler_min_full'])} ns "
                f"({'OK' if res.get('fit_success') else 'partial'})"
            )
        except Exception as e:
            import traceback

            print(f"[WARN] Analysis failed for {qp.name}: {e}")
            traceback.print_exc()
            node.results["results"][qp.name] = {
                "flux_coupler_min": np.nan,
                "flux_coupler_min_full": np.nan,
                "flux_qubit_max": np.nan,
                "flux_coupler_max": np.nan,
                "flux_coupler_max_full": np.nan,
                "fit_success": False,
            }

# %% {Plotting}
if not node.parameters.simulate:
    figures = plot_qubit_flux_duration_maps(
        ds,
        qubit_pairs,
        node.results["results"],
        use_state_discrimination=node.parameters.use_state_discrimination,
        fits=flux_fits_qp if node.parameters.analysis_debug else None,
        analysis_debug=node.parameters.analysis_debug,
    )
    for key, fig in figures.items():
        plt.show()
        node.results[key] = fig

# %% {Update_state}
if not node.parameters.simulate and node.parameters.load_data_id is None:
    with node.record_state_updates():
        for qp in qubit_pairs:
            res = node.results["results"][qp.name]
            if not np.isfinite(res.get("flux_qubit_max", np.nan)):
                print(f"[WARN] Skipping state update for {qp.name}: fit returned NaN")
                continue
            operation_name, coupler_attr = resolve_operation(qp)
            gate = qp.gates[operation_name]

            qp.detuning = res["flux_qubit_max"]
            gate.flux_pulse_control.amplitude = res["flux_qubit_max"]

            # Coupler amplitude stays at the fixed gate value; only duration is updated.
            gate_duration = res.get("flux_coupler_max_full", np.nan)
            if np.isfinite(gate_duration):
                pulse_length = int(np.ceil(float(gate_duration) / 4) * 4)
                gate.flux_pulse_control.length = pulse_length
                getattr(gate, coupler_attr).length = pulse_length
            else:
                print(f"[WARN] Skipping duration update for {qp.name}: fit returned NaN")

# %% {Save_results}
if not node.parameters.simulate:
    node.outcomes = {q.name: "successful" for q in qubit_pairs}
    node.results["initial_parameters"] = node.parameters.model_dump()
    node.save()
# %%
