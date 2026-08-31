# %% {Imports}
import math

from qualibrate import QualibrationNode, NodeParameters

from qualibrate import QualibrationNode, NodeParameters
from quam_libs.components import QuAM
from quam_libs.macros import active_reset, readout_state, readout_state_gef, active_reset_gef, active_reset_simple
from quam_libs.lib.save_utils import (
    fetch_results_as_xarray,
    restore_load_data_id,
    resolve_qubit_pairs_from_node,
)
from qualang_tools.results import progress_counter, fetching_tool
from qualang_tools.loops import from_array
from qualang_tools.multi_user import qm_session
from qualang_tools.units import unit
from qm import SimulationConfig
from qm.qua import *
from typing import Literal, Optional, List
import matplotlib.pyplot as plt
import numpy as np
from quam_libs.lib.fit import fit_oscillation, oscillation, fix_oscillation_phi_2pi
from quam_libs.lib.plot_utils import QubitPairGrid, grid_iter, grid_pair_names
from quam.components.pulses import FlatTopGaussianPulse, SquarePulse
from quam_libs.lib.pulses import CosineBipolarPulse, CosineFlatTopPulse
from calibration_utils.flux_landscape_fitting import (
    fit_coupler_zeropoint_pair,
    fit_coupler_zeropoint_to_legacy_results,
    plot_coupler_zeropoint_maps,
)

# %% {Description}
description = """
        COUPLER ZERO-INTERACTION CALIBRATION
This calibration program determines the flux bias point for tunable couplers that
results in zero effective coupling (g ≈ 0) between pairs of flux-tunable qubits.
This is a crucial step for architectures relying on dynamically tunable coupling
to implement high-fidelity two-qubit gates and isolate qubits during single-qubit operations.

The method performs a 2D sweep of:
    - The coupler flux bias (around its idle point).
    - The qubit control flux (to bring qubit frequencies closer to resonance).

Each point in this sweep involves initializing the control qubit in the excited state and applying
the selected two-qubit gate (with scaled qubit/coupler flux amplitudes). The resulting excitation in the
target qubit is measured either using state discrimination or IQ integration, depending on the
configuration. The aim is to identify the coupler bias point at which the residual interaction vanishes.

From the data, the optimal coupler flux (yielding minimal excitation transfer) and corresponding
control qubit flux (yielding maximal excitation retention) are extracted. These values are used
to update the coupler’s `decouple_offset` and the estimated qubit `detuning`.

This procedure ensures precise decoupling between qubits during idle or single-qubit operations, helping
mitigate unwanted crosstalk and residual ZZ interactions.

Prerequisites:
    - Coupler hardware model with known calibration structure.
    - Qubit frequencies, flux tuning models (quadratic term at least).
    - Active reset routines for fast initialization (optional).
    - Calibrated readout and XY pulses on the control and target qubits.
    - Initial coupler `decouple_offset` set near its expected g ≈ 0 point.

GEF readout (CZ only) is auto-selected from idle frequencies, same as 61y:
    control < target → GEF on target; control > target → GEF on control.

State update:
    - Selected gate flux amplitudes and pulse length: `qubit_pair.gates[operation]`
    - Control qubit detuning (rough estimate): `qubit_pair.detuning`

Additional notes:
    - Supports both simulation and hardware execution.
    - Results are visualized in a 2D map with overlays for idle and calibrated zero-g coupler flux points.
    - If enabled, detuning is also plotted on a secondary axis for interpretation.

This calibration is essential for optimizing gate scheduling, minimizing idling errors,
and preparing the system for entangling gate calibration.
"""

# %% {Node_parameters}
qubit_pair_indexes = [7]  # [1, 2]


class Parameters(NodeParameters):
    qubit_pairs: Optional[List[str]] = ["coupler_q%s_q%s" % (i, i + 1) for i in qubit_pair_indexes]  # ["coupler_q1_q2"]
    num_averages: int = 500
    flux_point_joint_or_independent_or_pairwise: Literal["joint", "independent", "pairwise"] = "joint"
    reset_type: Literal["active", "thermal"] = "active"
    simulate: bool = False
    timeout: int = 200
    load_data_id: Optional[int] = None

    coupler_flux_min: float = -0.2  # absolute coupler flux amplitude [V]
    coupler_flux_max: float =  0.0  # absolute coupler flux amplitude [V]
    coupler_flux_step: float = 0.005

    qubit_flux_span: float = 0.02  # relative to the selected gate qubit flux
    qubit_flux_step: float = 0.0005
    guess_flux_detuning: float|None = None  # fallback qubit flux center if the gate amplitude is missing
    use_state_discrimination: bool = True
    pulse_duration_ns: Optional[int] = None
    """Gate pulse length [ns]. If None, use flux_pulse_control.length from state.json."""
    cz_or_iswap: Literal["cz", "iswap"] = "cz"
    operation: Literal["Cz_flattop", "Cz_unipolar", "Cz_bipolar", "Cz_bipolar_slepian"] = "Cz_flattop"
    """CZ gate variant to calibrate. Ignored when cz_or_iswap is 'iswap'."""
    use_saved_detuning: bool = False  # fallback qubit flux center when gate amplitude is missing

    analysis_fit_preset: Literal["default", "noisy", "coarse"] = "default"
    """Contrast-cut fit preset (Savitzky–Golay + sliding-window FFT)."""
    analysis_debug: bool = True
    """If True, also plot the 1D contrast-cut diagnostic figure."""


node = QualibrationNode(name="61xx_coupler_zeropoint_pulse_calibration_advanced_fitting", parameters=Parameters())
assert not (
    node.parameters.simulate and node.parameters.load_data_id is not None
), "If simulate is True, load_data_id must be None, and vice versa."

# %% {Initialize_QuAM_and_QOP}
# Class containing tools to help handling units and conversions.
u = unit(coerce_to_integer=True)
# Instantiate the QuAM class from the state file
machine = QuAM.load()
node.machine = machine

# Get the relevant QuAM components
if node.parameters.qubit_pairs is None or node.parameters.qubit_pairs == "":
    qubit_pairs = machine.active_qubit_pairs
else:
    qubit_pairs = [machine.qubit_pairs[qp] for qp in node.parameters.qubit_pairs]
# if any([qp.q1.z is None or qp.q2.z is None for qp in qubit_pairs]):
#     warnings.warn("Found qubit pairs without a flux line. Skipping")

num_qubit_pairs = len(qubit_pairs)
# %%

####################
# Helper functions #
####################


def gef_on_target_qubit(qp):
    """GEF the higher-frequency qubit (same frequency rule as 61y).

    control < target → GEF on target (former con_tar_flip=True)
    control > target → GEF on control (former con_tar_flip=False)
    """
    f_c = float(qp.qubit_control.xy.RF_frequency)
    f_t = float(qp.qubit_target.xy.RF_frequency)
    on_target = f_c < f_t
    gef_qubit = qp.qubit_target if on_target else qp.qubit_control
    print(
        f"{qp.name}: f_control={qp.qubit_control.name} {f_c / 1e9:.4f} GHz, "
        f"f_target={qp.qubit_target.name} {f_t / 1e9:.4f} GHz → "
        f"GEF on {gef_qubit.name} ({'target' if on_target else 'control'})"
    )
    return on_target


def resolve_operation(qp):
    """Return (operation_name, coupler_pulse_attr) for the configured gate variant."""
    if node.parameters.cz_or_iswap == "iswap":
        return "SWAP", "coupler_pulse_control"
    return node.parameters.operation, "coupler_flux_pulse"


def qubit_flux_center(qp, operation_name):
    """Qubit flux sweep center from the selected gate, with fallbacks for first calibration."""
    # gate_amp = qp.gates[operation_name].flux_pulse_control.amplitude
    # if gate_amp is not None and gate_amp != 0.0:
    #     print(f"Gate amplitude: {gate_amp}")
    #     return gate_amp
    if node.parameters.guess_flux_detuning is not None:
        return node.parameters.guess_flux_detuning
    if node.parameters.use_saved_detuning and qp.detuning is not None:
        return qp.detuning
    if node.parameters.cz_or_iswap == "iswap":
        return np.sqrt(
            -(qp.qubit_control.xy.RF_frequency - qp.qubit_target.xy.RF_frequency)
            / qp.qubit_control.freq_vs_flux_01_quad_term
        )
    # check if need to switch target and control, also check if detune is greater than anharmonicity
    if qp.qubit_control.xy.RF_frequency > qp.qubit_target.xy.RF_frequency and qp.qubit_control.xy.RF_frequency - qp.qubit_target.xy.RF_frequency > qp.qubit_control.anharmonicity:
        qubit_flux_center = np.sqrt(
            -(qp.qubit_control.xy.RF_frequency - qp.qubit_target.xy.RF_frequency - qp.qubit_control.anharmonicity)
            / qp.qubit_target.freq_vs_flux_01_quad_term
        )
        return qubit_flux_center
    elif qp.qubit_control.xy.RF_frequency < qp.qubit_target.xy.RF_frequency and qp.qubit_target.xy.RF_frequency - qp.qubit_control.xy.RF_frequency < qp.qubit_target.anharmonicity:
        qubit_flux_center = np.sqrt(
            -(qp.qubit_control.xy.RF_frequency - qp.qubit_target.xy.RF_frequency + qp.qubit_target.anharmonicity)
            / qp.qubit_control.freq_vs_flux_01_quad_term
        )
        return qubit_flux_center
    else:
        raise ValueError("You should switch target and control")


def resolve_pulse_duration_ns(qp, operation_name):
    """Return configured pulse duration, or gate flux_pulse_control.length from state."""
    if node.parameters.pulse_duration_ns is not None:
        return int(np.ceil(node.parameters.pulse_duration_ns / 4) * 4)
    length = qp.gates[operation_name].flux_pulse_control.length
    if length is None:
        raise ValueError(f"{qp.name}: gate {operation_name} flux_pulse_control.length is missing in state")
    return int(length)


def resolve_coupler_pulse_duration_ns(qp, operation_name, coupler_attr):
    """Return configured pulse duration, or coupler flux pulse length from state."""
    if node.parameters.pulse_duration_ns is not None:
        return int(np.ceil(node.parameters.pulse_duration_ns / 4) * 4)
    length = getattr(qp.gates[operation_name], coupler_attr).length
    if length is None:
        raise ValueError(f"{qp.name}: gate {operation_name} {coupler_attr}.length is missing in state")
    return int(length)


gef_on_target = {qp.name: gef_on_target_qubit(qp) for qp in qubit_pairs}

# %% {QUA_program}
n_avg = node.parameters.num_averages  # The number of averages

flux_point = node.parameters.flux_point_joint_or_independent_or_pairwise  # 'independent' or 'joint' or 'pairwise'
# Coupler flux sweep uses absolute amplitudes; qubit flux is relative to its center.
fluxes_coupler = np.arange(
    node.parameters.coupler_flux_min, node.parameters.coupler_flux_max + 0.0001, node.parameters.coupler_flux_step
)

fluxes_qubit = np.arange(
    -node.parameters.qubit_flux_span / 2, node.parameters.qubit_flux_span / 2 + 0.0001, node.parameters.qubit_flux_step
)
fluxes_qp = {}
fluxes_coupler_qp = {}
coupler_centers_qp = {}
GATE_AMP_FALLBACK = 0.5
for qp in qubit_pairs:
    operation_name, coupler_attr = resolve_operation(qp)
    gate = qp.gates[operation_name]
    gate.flux_pulse_control.amplitude = GATE_AMP_FALLBACK
    coupler_pulse = getattr(gate, coupler_attr)
    coupler_pulse.amplitude = GATE_AMP_FALLBACK
    qubit_center = qubit_flux_center(qp, operation_name)
    coupler_centers_qp[qp.name] = getattr(gate, coupler_attr).amplitude
    fluxes_qp[qp.name] = fluxes_qubit + qubit_center
    fluxes_coupler_qp[qp.name] = fluxes_coupler

config = machine.generate_config()
octave_config = machine.get_octave_config()
if node.parameters.load_data_id is None:
    qmm = machine.connect()

for qp in qubit_pairs:
    operation_name, _ = resolve_operation(qp)
    duration_ns = resolve_pulse_duration_ns(qp, operation_name)
    assert duration_ns % 4 == 0, (
        f"Expected pulse duration to be divisible by 4, got {duration_ns} ns for {qp.name}"
    )

reset_coupler_bias = False

with program() as coupler_zero_point_calibration:
    n = declare(int)
    flux_coupler = declare(float)
    flux_qubit = declare(float)
    comp_flux_qubit = declare(float)
    comp_flux_target_qubit = declare(float)
    comp_flux_coupler = declare(float)
    n_st = declare_stream()
    amp_qubit = declare(float)
    amp_coupler = declare(float)
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
        operation_name, coupler_attr = resolve_operation(qp)
        gate = qp.gates[operation_name]
        qubit_gate_amp = gate.flux_pulse_control.amplitude
        coupler_gate_amp = getattr(gate, coupler_attr).amplitude
        qp.gates[operation_name].phase_shift_control = 0.0
        qp.gates[operation_name].phase_shift_target = 0.0
        print("qubit control: %s, qubit target: %s" % (qp.qubit_control.name, qp.qubit_target.name))
        # Bring the active qubits to the minimum frequency point
        machine.set_all_fluxes(flux_point, qp)
        if reset_coupler_bias:
            qp.coupler.set_dc_offset(0.0)
        else:
            qp.coupler.to_decouple_idle()
        wait(1000)

        with for_(n, 0, n < n_avg, n + 1):
            save(n, n_st)
            with for_each_(flux_coupler, fluxes_coupler_qp[qp.name]):
                with for_each_(flux_qubit, fluxes_qp[qp.name]):
                    # reset
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
                        assign(comp_flux_qubit, flux_qubit + qp.extras["coupler_qubit_crosstalk"] * flux_coupler)
                    else:
                        print("No crosstalk compensated")
                        assign(comp_flux_qubit, flux_qubit)
                    # setting both qubits ot the initial state
                    qp.qubit_control.xy.play("x180")
                    if node.parameters.cz_or_iswap == "cz":
                        qp.qubit_target.xy.play("x180")
                    align()
                    assign(amp_qubit, comp_flux_qubit / qubit_gate_amp)
                    assign(amp_coupler, flux_coupler / coupler_gate_amp)
                    qp.gates[operation_name].execute(
                        amplitude_scale=amp_qubit,
                        coupler_amplitude_scale=amp_coupler,
                    )
                    align()
                    wait(20)
                    # readout
                    if node.parameters.use_state_discrimination:
                        if node.parameters.cz_or_iswap == "cz":
                            if gef_on_target[qp.name]:
                                readout_state(qp.qubit_control, state_control[i])
                                readout_state_gef(qp.qubit_target, state_target[i])
                            else:
                                readout_state_gef(qp.qubit_control, state_control[i])
                                readout_state(qp.qubit_target, state_target[i])
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
                state_st_control[i].buffer(len(fluxes_qubit)).buffer(len(fluxes_coupler)).average().save(
                    f"state_control{i + 1}"
                )
                state_st_target[i].buffer(len(fluxes_qubit)).buffer(len(fluxes_coupler)).average().save(
                    f"state_target{i + 1}"
                )
                state_st[i].buffer(len(fluxes_qubit)).buffer(len(fluxes_coupler)).average().save(f"state{i + 1}")
            else:
                I_st_control[i].buffer(len(fluxes_qubit)).buffer(len(fluxes_coupler)).average().save(
                    f"I_control{i + 1}"
                )
                Q_st_control[i].buffer(len(fluxes_qubit)).buffer(len(fluxes_coupler)).average().save(
                    f"Q_control{i + 1}"
                )
                I_st_target[i].buffer(len(fluxes_qubit)).buffer(len(fluxes_coupler)).average().save(f"I_target{i + 1}")
                Q_st_target[i].buffer(len(fluxes_qubit)).buffer(len(fluxes_coupler)).average().save(f"Q_target{i + 1}")

# %% {Simulate_or_execute}
if node.parameters.simulate:
    # Simulates the QUA program for the specified duration
    simulation_config = SimulationConfig(duration=10_000)  # In clock cycles = 4ns
    job = qmm.simulate(config, coupler_zero_point_calibration, simulation_config)
    samples = job.get_simulated_samples()
    fig, ax = plt.subplots(nrows=len(samples.keys()), sharex=True)
    for i, con in enumerate(samples.keys()):
        plt.subplot(len(samples.keys()), 1, i + 1)
        samples[con].plot()
        plt.title(con)
    plt.tight_layout()
    wf_report = job.get_simulated_waveform_report()
    wf_report.create_plot(samples, plot=True, save_path=None)
    node.results = {"figure": plt.gcf()}
    node.save()
elif node.parameters.load_data_id is None:
    with qm_session(qmm, config, timeout=node.parameters.timeout) as qm:
        from qm import generate_qua_script

        with open("debug.py", "w+") as f:
            f.write(generate_qua_script(coupler_zero_point_calibration, config))
        job = qm.execute(coupler_zero_point_calibration)

        results = fetching_tool(job, ["n"], mode="live")
        while results.is_processing():
            # Fetch results
            n = results.fetch_all()[0]
            # Progress bar
            progress_counter(n, n_avg, start_time=results.start_time)

# %% {Data_fetching_and_dataset_creation}
if not node.parameters.simulate:
    if node.parameters.load_data_id is None:
        # Fetch the data from the OPX and convert it into a xarray with corresponding axes (from most inner to outer loop)
        ds = fetch_results_as_xarray(
            job.result_handles, qubit_pairs, {"flux_qubit": fluxes_qubit, "flux_coupler": fluxes_coupler}
        )
        flux_qubit_full = np.array([fluxes_qp[qp.name] for qp in qubit_pairs])
        flux_coupler_full = np.array([fluxes_coupler_qp[qp.name] + qp.coupler.decouple_offset for qp in qubit_pairs])
        ds = ds.assign_coords({"flux_qubit_full": (["qubit", "flux_qubit"], flux_qubit_full)})
        ds = ds.assign_coords({"flux_coupler_full": (["qubit", "flux_coupler"], flux_coupler_full)})
    else:
        load_data_id = node.parameters.load_data_id
        node = node.load_from_id(load_data_id)
        ds = node.results["ds"]
        restore_load_data_id(node, load_data_id)
        machine = node.machine
        qubit_pairs = resolve_qubit_pairs_from_node(machine, node)
        gef_on_target = {qp.name: gef_on_target_qubit(qp) for qp in qubit_pairs}
        coupler_centers_qp = {}
        for qp in qubit_pairs:
            operation_name, coupler_attr = resolve_operation(qp)
            coupler_centers_qp[qp.name] = getattr(qp.gates[operation_name], coupler_attr).amplitude

    node.results = {
        "ds": ds,
        "gef_on_target": {k: bool(v) for k, v in gef_on_target.items()},
    }

# %% Data processing
detuning_mode = "quadratic"  # "cosine" or "quadratic"
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
        fluxes_coupler = np.arange(
            node.parameters.coupler_flux_min,
            node.parameters.coupler_flux_max + 0.0001,
            node.parameters.coupler_flux_step,
        )
        fluxes_qp = {}
        fluxes_coupler_qp = {}
        for qp in qubit_pairs:
            operation_name, coupler_attr = resolve_operation(qp)
            gate = qp.gates[operation_name]
            fluxes_qp[qp.name] = fluxes_qubit + qubit_flux_center(qp, operation_name)
            fluxes_coupler_qp[qp.name] = fluxes_coupler
        flux_qubit_full = np.array([fluxes_qp[qp.name] for qp in qubit_pairs])
        flux_coupler_full = np.array(
            [fluxes_coupler_qp[qp.name] + qp.coupler.decouple_offset for qp in qubit_pairs]
        )
        ds = ds.assign_coords({"flux_qubit_full": (["qubit", "flux_qubit"], flux_qubit_full)})
        ds = ds.assign_coords({"flux_coupler_full": (["qubit", "flux_coupler"], flux_coupler_full)})
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
    node.results = {
        "ds": ds,
        "gef_on_target": {k: bool(v) for k, v in gef_on_target.items()},
    }

# Ensure flux coords exist before analysis (e.g. when re-running cells on loaded data)
if not node.parameters.simulate and (
    "flux_qubit_full" not in ds.coords or "flux_coupler_full" not in ds.coords
):
    if "fluxes_qp" not in globals():
        _fluxes_qubit = np.arange(
            -node.parameters.qubit_flux_span / 2,
            node.parameters.qubit_flux_span / 2 + 0.0001,
            node.parameters.qubit_flux_step,
        )
        _fluxes_coupler = np.arange(
            node.parameters.coupler_flux_min,
            node.parameters.coupler_flux_max + 0.0001,
            node.parameters.coupler_flux_step,
        )
        fluxes_qp = {}
        fluxes_coupler_qp = {}
        for qp in qubit_pairs:
            operation_name, coupler_attr = resolve_operation(qp)
            gate = qp.gates[operation_name]
            fluxes_qp[qp.name] = _fluxes_qubit + qubit_flux_center(qp, operation_name)
            fluxes_coupler_qp[qp.name] = _fluxes_coupler
    flux_qubit_full = np.array([fluxes_qp[qp.name] for qp in qubit_pairs])
    flux_coupler_full = np.array(
        [fluxes_coupler_qp[qp.name] + qp.coupler.decouple_offset for qp in qubit_pairs]
    )
    ds = ds.assign_coords({"flux_qubit_full": (["qubit", "flux_qubit"], flux_qubit_full)})
    ds = ds.assign_coords({"flux_coupler_full": (["qubit", "flux_coupler"], flux_coupler_full)})
    node.results["ds"] = ds

# %% Data Analysis
if not node.parameters.simulate:
    if "coupler_centers_qp" not in globals():
        coupler_centers_qp = {}
        for qp in qubit_pairs:
            operation_name, coupler_attr = resolve_operation(qp)
            coupler_centers_qp[qp.name] = getattr(qp.gates[operation_name], coupler_attr).amplitude

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
            coupler_center = coupler_centers_qp.get(qp.name)
            if coupler_center is None:
                operation_name, coupler_attr = resolve_operation(qp)
                coupler_center = getattr(qp.gates[operation_name], coupler_attr).amplitude
            flux_fits_qp[qp.name] = fit
            node.results["results"][qp.name] = fit_coupler_zeropoint_to_legacy_results(
                fit,
                decouple_offset=float(qp.coupler.decouple_offset),
                coupler_center=coupler_center,
            )
            res = node.results["results"][qp.name]
            def _mv(v):
                return f"{v * 1e3:.1f}" if np.isfinite(v) else "NaN"
            print(
                f"{qp.name}: decouple={_mv(res['flux_coupler_min_full'])} mV, "
                f"qubit={_mv(res['flux_qubit_max'])} mV, "
                f"gate={_mv(res['flux_coupler_max_full'])} mV "
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
    figures = plot_coupler_zeropoint_maps(
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

#  %% {Update_state}
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
            if np.isfinite(res.get("flux_coupler_max", np.nan)):
                getattr(gate, coupler_attr).amplitude = res["flux_coupler_max"]
            gate.flux_pulse_control.length = resolve_pulse_duration_ns(qp, operation_name)
            getattr(gate, coupler_attr).length = resolve_coupler_pulse_duration_ns(
                qp, operation_name, coupler_attr
            )

 # %% {Save_results}
if not node.parameters.simulate:
    node.outcomes = {q.name: "successful" for q in qubit_pairs}
    node.results["initial_parameters"] = node.parameters.model_dump()
    node.save()
# %%
