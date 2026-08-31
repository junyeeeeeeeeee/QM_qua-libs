# %% {Imports}
from dataclasses import asdict

import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from calibration_utils.cz_conditional_phase_error_amp import (
    fit_raw_data,
    log_fitted_results,
    plot_raw_data_with_fit,
    process_raw_dataset,
)
from qm.qua import *
from qualang_tools.loops import from_array
from qualang_tools.multi_user import qm_session
from qualang_tools.results import progress_counter
from qualang_tools.units import unit
from qualibrate import QualibrationNode
from quam_libs.lib.save_utils import (
    fetch_results_as_xarray,
    restore_load_data_id,
    resolve_qubit_pairs_from_node,
)
from qm import SimulationConfig
from quam_libs.components import QuAM
from qualibrate import QualibrationNode, NodeParameters
from typing import Literal, Optional, List
from quam_libs.macros import active_reset, readout_state_gef, active_reset_gef
from qualang_tools.results import progress_counter, fetching_tool

# %% {Initialisation}
description = """
CALIBRATION OF THE CONTROLLED-PHASE (CPHASE) OF THE CZ GATE with error amplification (coupler amplitude sweep)

This sequence calibrates the CPhase of the CZ gate by scanning the coupler pulse amplitude and measuring the
resulting phase of the target qubit. The calibration compares two scenarios:

1. Control qubit in the ground state
2. Control qubit in the excited state

For each amplitude, we measure:
1. The phase difference of the target qubit between the two scenarios
2. The average population in the |g>, |e>, and |f> states of the control qubit when the control qubit is in the excited state.

**Error amplification:**
To improve sensitivity to small phase errors, the CZ gate is applied repeatedly (multiple times in sequence) for each measurement. This introduces an extra dimension to the experiment: the number of repeated CZ operations. By increasing the number of repetitions, small phase errors accumulate, making them easier to detect and fit.

The calibration process involves:
1. Applying a CZ gate with varying coupler amplitudes
2. Repeating the CZ operation a variable number of times (error amplification dimension)
3. Measuring the phase of the target qubit for both control qubit states
4. Calculating the phase difference
5. Measuring the population fractions of the |g>, |e>, and |f> states on the control qubit to quantify leakage

The optimal CZ gate amplitude is determined by finding the point where:
1. The phase difference (after error amplification) is closest to π (0.5 in normalized units)
2. The leakage to the |f> state is minimized

Prerequisites:
- Calibrated single-qubit gates for both qubits in the pair
- Calibrated readout for both qubits
- Initial estimate of the CZ gate coupler amplitude

State update:
- The optimal CZ gate coupler amplitude: qubit_pair.gates[operation].coupler_flux_pulse.amplitude
"""

# %% {Parameters}
qubit_pair_indexes = [3]  # The indexes of the qubit pair to calibrate
class Parameters(NodeParameters):
    qubit_pairs: Optional[List[str]] = ["coupler_q%s_q%s" % (i, i + 1) for i in qubit_pair_indexes]
    num_averages: int = 50
    """Number of averages to perform. Default is 50."""
    amp_range: float = 0.05
    """Range of amplitude variation around the nominal value, will scan between center - range and center + range."""
    amp_step: float = 0.002
    """Step size for amplitude scanning."""
    num_frame_rotations: int = 13
    """Number of frame rotation points for phase measurement."""
    operation: Literal["Cz_flattop", "Cz_unipolar", "Cz_bipolar"] = "Cz"
    """Type of CZ operation to perform."""
    number_of_operations: int = 12
    """Number of operations to perform for each amplitude."""
    flux_point_joint_or_independent: Literal["joint", "independent"] = "joint"
    load_data_id: Optional[int] = None
    reset_type: Literal["thermal", "active"] = "active"
    use_state_discrimination: bool = True
    simulate: bool = False
    simulation_duration_ns: int = 1500
    timeout: int = 100

node = QualibrationNode(
    name="32bx_cz_phase_coupler_error_amp", parameters=Parameters()
)

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

# %% {QUA_program}
node.namespace["qubit_pairs"] = qubit_pairs
node.namespace["amplitude_sweep"] = "coupler"
n_avg = node.parameters.num_averages
amplitudes = np.arange(1 - node.parameters.amp_range, 1 + node.parameters.amp_range, node.parameters.amp_step)
frames = np.arange(0, 1, 1 / node.parameters.num_frame_rotations)

operation_name = node.parameters.operation
num_operations = node.parameters.number_of_operations

gate_refs = {}
for qp in qubit_pairs:
    gate_refs[qp.name] = {
        "coupler_amplitude": qp.gates[operation_name].coupler_flux_pulse.amplitude,
    }
node.namespace["gate_refs"] = gate_refs
node.namespace["sweep_axes"] = {
    "qubit_pair": xr.DataArray([qp.id for qp in qubit_pairs], attrs={"long_name": "qubit pair index"}),
    "number_of_operations": xr.DataArray(
        np.arange(1, num_operations + 1),
        attrs={"long_name": "number of operations"},
    ),
    "amp": xr.DataArray(amplitudes, attrs={"long_name": "amplitude scale", "units": "a.u."}),
    "frame": xr.DataArray(frames, attrs={"long_name": "frame rotation", "units": "2π"}),
    "control_axis": xr.DataArray([0, 1], attrs={"long_name": "control qubit state"}),
}
flux_point = node.parameters.flux_point_joint_or_independent

with program() as CZ_phase_calibration_coupler_error_amp:
    amp = declare(fixed)
    frame = declare(fixed)
    frame_odd = declare(fixed)
    control_initial = declare(int)
    n = declare(int)
    n_op = declare(int)
    count = declare(int)
    n_st = declare_stream()
    I_c = [declare(fixed) for _ in range(num_qubit_pairs)]
    Q_c = [declare(fixed) for _ in range(num_qubit_pairs)]
    I_c_st = [declare_stream() for _ in range(num_qubit_pairs)]
    Q_c_st = [declare_stream() for _ in range(num_qubit_pairs)]
    I_t = [declare(fixed) for _ in range(num_qubit_pairs)]
    Q_t = [declare(fixed) for _ in range(num_qubit_pairs)]
    I_t_st = [declare_stream() for _ in range(num_qubit_pairs)]
    Q_t_st = [declare_stream() for _ in range(num_qubit_pairs)]
    if node.parameters.use_state_discrimination:
        state_c = [declare(int) for _ in range(num_qubit_pairs)]
        state_t = [declare(int) for _ in range(num_qubit_pairs)]
        state_c_st = [declare_stream() for _ in range(num_qubit_pairs)]
        state_t_st = [declare_stream() for _ in range(num_qubit_pairs)]
    for i, qp in enumerate(qubit_pairs):
        qp.gates[operation_name].phase_shift_control = 0.0
        qp.gates[operation_name].phase_shift_target = 0.0
        if not node.parameters.simulate:
            machine.set_all_fluxes(flux_point, qp)
        with for_(n, 0, n < n_avg, n + 1):
            save(n, n_st)
            with for_(n_op, 1, n_op <= num_operations, n_op + 1):
                with for_(*from_array(amp, amplitudes)):
                    with for_(*from_array(frame, frames)):
                        with for_(*from_array(control_initial, [0, 1])):
                            if not node.parameters.simulate:
                                if node.parameters.reset_type == "active":
                                    active_reset_gef(qp.qubit_control)
                                    active_reset(qp.qubit_target)
                                else:
                                    wait(qp.qubit_control.thermalization_time * u.ns)
                            qp.align()
                            reset_frame(qp.qubit_target.xy.name)
                            reset_frame(qp.qubit_control.xy.name)
                            qp.qubit_control.xy.play("x180", condition=control_initial == 1)
                            qp.qubit_target.xy.play("x90")
                            qp.align()
                            with for_(count, 0, count < n_op, count + 1):
                                qp.gates[operation_name].execute(coupler_amplitude_scale=amp)
                                qp.align()
                            with if_(((n_op & 1) == 0) & (control_initial == 1)):
                                assign(frame_odd, frame - 0.5)
                                qp.qubit_target.xy.frame_rotation_2pi(frame_odd)
                            with else_():
                                qp.qubit_target.xy.frame_rotation_2pi(frame)
                            qp.qubit_target.xy.play("x90")
                            qp.align()

                            if node.parameters.use_state_discrimination:
                                readout_state_gef(qp.qubit_control, state_c[i])
                                readout_state_gef(qp.qubit_target, state_t[i])
                                save(state_c[i], state_c_st[i])
                                save(state_t[i], state_t_st[i])
                            else:
                                qp.qubit_control.resonator.measure("readout", qua_vars=(I_c[i], Q_c[i]))
                                qp.qubit_target.resonator.measure("readout", qua_vars=(I_t[i], Q_t[i]))
                                save(I_c[i], I_c_st[i])
                                save(Q_c[i], Q_c_st[i])
                                save(I_t[i], I_t_st[i])
                                save(Q_t[i], Q_t_st[i])
        align()

    with stream_processing():
        n_st.save("n")
        for i in range(num_qubit_pairs):
            if node.parameters.use_state_discrimination:
                state_c_st[i].buffer(2).buffer(len(frames)).buffer(len(amplitudes)).buffer(num_operations).average().save(
                    f"state_control{i + 1}"
                )
                state_t_st[i].buffer(2).buffer(len(frames)).buffer(len(amplitudes)).buffer(num_operations).average().save(
                    f"state_target{i + 1}"
                )
            else:
                I_c_st[i].buffer(2).buffer(len(frames)).buffer(len(amplitudes)).buffer(num_operations).average().save(
                    f"I_control{i + 1}"
                )
                Q_c_st[i].buffer(2).buffer(len(frames)).buffer(len(amplitudes)).buffer(num_operations).average().save(
                    f"Q_control{i + 1}"
                )
                I_t_st[i].buffer(2).buffer(len(frames)).buffer(len(amplitudes)).buffer(num_operations).average().save(
                    f"I_target{i + 1}"
                )
                Q_t_st[i].buffer(2).buffer(len(frames)).buffer(len(amplitudes)).buffer(num_operations).average().save(
                    f"Q_target{i + 1}"
                )

# %% {Simulate}
if node.parameters.simulate:
    simulation_config = SimulationConfig(duration=10_000)
    job = qmm.simulate(config, CZ_phase_calibration_coupler_error_amp, simulation_config)
    job.get_simulated_samples().con1.plot()
    node.results = {"figure": plt.gcf()}
    node.save()
elif node.parameters.load_data_id is None:
    with qm_session(qmm, config, timeout=node.parameters.timeout) as qm:
        job = qm.execute(CZ_phase_calibration_coupler_error_amp)

        results = fetching_tool(job, ["n"], mode="live")
        while results.is_processing():
            n = results.fetch_all()[0]
            progress_counter(n, n_avg, start_time=results.start_time)

# %% {Data_fetching_and_dataset_creation}
if not node.parameters.simulate:
    if node.parameters.load_data_id is None:
        ds = fetch_results_as_xarray(
            job.result_handles,
            qubit_pairs,
            {
                "control_axis": [0, 1],
                "frame": frames,
                "amp": amplitudes,
                "number_of_operations": np.arange(1, num_operations + 1),
            },
        )
    else:
        load_data_id = node.parameters.load_data_id
        node = node.load_from_id(load_data_id)
        ds = node.results["ds_raw"]
        restore_load_data_id(node, load_data_id)
        machine = node.machine
        operation_name = node.parameters.operation
        qubit_pairs = resolve_qubit_pairs_from_node(machine, node)
        node.namespace["qubit_pairs"] = qubit_pairs
        node.namespace["amplitude_sweep"] = "coupler"
        gate_refs = {}
        for qp in qubit_pairs:
            gate_refs[qp.name] = {
                "coupler_amplitude": qp.gates[operation_name].coupler_flux_pulse.amplitude,
            }
        node.namespace["gate_refs"] = gate_refs

    if "qubit" in ds.dims:
        ds = ds.rename({"qubit": "qubit_pair"})
    node.results = {"ds_raw": ds}

# %% {Analyse_data}
if not node.parameters.simulate:
    node.results["ds_raw"] = process_raw_dataset(node.results["ds_raw"], node)
    node.results["ds_fit"], fit_results = fit_raw_data(node.results["ds_raw"], node)
    node.results["fit_results"] = {k: asdict(v) for k, v in fit_results.items()}
    log_fitted_results(fit_results, log_callable=node.log)
    node.outcomes = {
        qubit_pair_name: ("successful" if fit_result.success else "failed")
        for qubit_pair_name, fit_result in fit_results.items()
    }

# %% {Plot_data}
if not node.parameters.simulate:
    qubit_pairs = node.namespace["qubit_pairs"]
    fig_phase = plot_raw_data_with_fit(
        node.results["ds_fit"],
        qubit_pairs,
        node=node,
    )
    plt.show()
    node.results["phase_figure"] = fig_phase

# %% {Update_state}
if not node.parameters.simulate:
    if node.parameters.load_data_id is None:
        with node.record_state_updates():
            fit_results = node.results["fit_results"]
            for qp in node.namespace["qubit_pairs"]:
                if node.outcomes[qp.name] == "failed":
                    continue
                qp.gates[operation_name].coupler_flux_pulse.amplitude = fit_results[qp.name]["optimal_amplitude"]

# %% {Save_results}
if not node.parameters.simulate:
    node.outcomes = {qp.name: "successful" for qp in qubit_pairs}
    node.results["initial_parameters"] = node.parameters.model_dump()
    node.save()

# %%
