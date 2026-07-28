"""
        T1 MEASUREMENT
The sequence consists in putting the qubit in the excited stated by playing the x180 pulse and measuring the resonator
after a varying time. The qubit T1 is extracted by fitting the exponential decay of the measured quadratures.

Prerequisites:
    - Having found the resonance frequency of the resonator coupled to the qubit under study (resonator_spectroscopy).
    - Having calibrated qubit pi pulse (x180) by running qubit spectroscopy, power_rabi and updated the state.
    - (optional) Having calibrated the readout (readout_frequency, amplitude, duration_optimization IQ_blobs) for better SNR.
    - Set the desired flux bias.

Next steps before going to the next node:
    - Update the qubit T1 in the state.
"""

# %% {Imports}
from qualibrate import QualibrationNode, NodeParameters
from quam_libs.components import QuAM
from quam_libs.macros import qua_declaration, readout_state, active_reset_simple
from quam_libs.lib.qua_datasets import convert_IQ_to_V
from quam_libs.lib.plot_utils import QubitGrid, grid_iter
from quam_libs.lib.save_utils import (
    fetch_results_as_xarray,
    restore_load_data_id,
    resolve_qubits_from_node,
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


# %% {Node_parameters}
class Parameters(NodeParameters):
    qubits: Optional[List[str]] = ["q3"]
    """List of qubits to measure. If None or empty, uses all control qubits in the qubit pair."""
    qubit_flux: Optional[List[str]] = ["q4"]
    """List of qubits to apply the flux pulse during idle time (can differ from measured qubits)."""
    qubit_pair: str = "coupler_q3_q4"
    """Qubit pair to use for the measurement."""
    num_averages: int = 100
    """Number of averages to perform."""
    min_wait_time_in_ns: int = 16
    """Minimum wait time in nanoseconds."""
    max_wait_time_in_ns: int = 2000
    """Maximum wait time in nanoseconds."""
    wait_time_step_in_ns: int = 40
    """Step size for wait time in nanoseconds."""
    flux_point_joint_or_independent_or_arbitrary: Literal["joint", "independent", "arbitrary"] = "joint"
    """Flux point setting for the qubits: 'joint', 'independent', or 'arbitrary'."""
    reset_type: Literal["active", "thermal"] = "thermal"
    """Type of reset to use before each measurement: 'active' or 'thermal'."""
    use_state_discrimination: bool = False
    """Whether to use state discrimination for readout."""
    simulate: bool = False
    """Whether to simulate the QUA program instead of executing it."""
    simulation_duration_ns: int = 10000
    """Duration of the simulation in nanoseconds."""
    timeout: int = 100
    """Timeout for QOP session in seconds."""
    load_data_id: Optional[int] = None
    """If provided, loads data from a previous run with this ID instead of executing the program."""
    multiplexed: bool = True
    """Whether to measure qubits in multiplexed mode."""
    reset_coupler_bias: bool = False
    """Whether to reset the coupler bias to zero before each measurement."""
    qubit_flux_min: float = -0.05  # relative to the qubit flux set point
    """Minimum qubit flux value."""
    qubit_flux_max: float = -0.038  # relative to the qubit flux set point
    """Maximum qubit flux value."""
    qubit_flux_num_points: int = 101
    """Number of qubit flux points."""
    use_qubit_flux_pulse: bool = True
    """Whether to use a flux pulse on qubit_flux during the idle time."""

node = QualibrationNode(name="05bx_T1_vs_qubit_flux", parameters=Parameters())


# %% {Initialize_QuAM_and_QOP}
# Class containing tools to help handle units and conversions.
u = unit(coerce_to_integer=True)
# Instantiate the QuAM class from the state file
machine = QuAM.load()
# Generate the OPX and Octave configurations
config = machine.generate_config()
# Open Communication with the QOP
if node.parameters.load_data_id is None:
    qmm = machine.connect()
    
qubit_pair = machine.qubit_pairs[node.parameters.qubit_pair]

# Get the relevant QuAM components
if node.parameters.qubits is None or node.parameters.qubits == "":
    qubits = qubit_pair.qubit_control
else:
    qubits = [machine.qubits[q] for q in node.parameters.qubits]

if node.parameters.qubit_flux is None or node.parameters.qubit_flux == "":
    flux_qubits = qubits
else:
    flux_qubits = [machine.qubits[q] for q in node.parameters.qubit_flux]

num_qubits = len(qubits)
flux_qubit_names = [q.name for q in flux_qubits]


# %% {QUA_program}
n_avg = node.parameters.num_averages  # The number of averages
# Dephasing time sweep (in clock cycles = 4ns) - minimum is 4 clock cycles
idle_times = np.arange(
    node.parameters.min_wait_time_in_ns // 4,
    node.parameters.max_wait_time_in_ns // 4,
    node.parameters.wait_time_step_in_ns // 4,
)

fluxes_qubit = np.linspace(
    node.parameters.qubit_flux_min,
    node.parameters.qubit_flux_max,
    node.parameters.qubit_flux_num_points,
)
reset_coupler_bias = node.parameters.reset_coupler_bias
flux_point = node.parameters.flux_point_joint_or_independent_or_arbitrary  # 'independent' or 'joint'

if flux_point == "arbitrary":
    detunings = {q.name: q.arbitrary_intermediate_frequency for q in qubits}
    arb_flux_bias_offset = {q.name: q.z.arbitrary_offset for q in flux_qubits}
else:
    arb_flux_bias_offset = {q.name: 0.0 for q in flux_qubits}
    detunings = {q.name: 0.0 for q in qubits}

with program() as t1_vs_qubit_flux:
    flux_qubit = declare(float)
    comp_flux_qubit = declare(float)
    I, I_st, Q, Q_st, n, n_st = qua_declaration(num_qubits=num_qubits)
    t = declare(int)  # QUA variable for the idle time
    if node.parameters.use_state_discrimination:
        state = [declare(int) for _ in range(num_qubits)]
        state_st = [declare_stream() for _ in range(num_qubits)]

    for i, qubit in enumerate(qubits):
        XY_delay = qubit.xy.opx_output.delay + 4
        # Bring the active qubits to the desired frequency point
        machine.set_all_fluxes(flux_point=flux_point, target=qubit)
        for flux_q in flux_qubits:
            machine.set_all_fluxes(flux_point=flux_point, target=flux_q)
        if "c" in qubit.id: qubit.z.set_dc_offset(qubit.z.joint_offset) # for coupler-test case
        for flux_q in flux_qubits:
            if "c" in flux_q.id: flux_q.z.set_dc_offset(flux_q.z.joint_offset)
        qubit.z.settle()
        for flux_q in flux_qubits:
            flux_q.z.settle()
        qubit.align()
        
        if reset_coupler_bias:
            qubit_pair.coupler.set_dc_offset(0.0)
        else:
            qubit_pair.coupler.to_decouple_idle()
        wait(1000)

        with for_(n, 0, n < n_avg, n + 1):
            save(n, n_st)
            with for_(*from_array(flux_qubit, fluxes_qubit)):
                with for_(*from_array(t, idle_times)):
                    if node.parameters.reset_type == "active":
                        # active_reset(qubit, "readout")
                        active_reset_simple(qubit, "readout")
                        qubit_pair.align()
                    else:
                        qubit.resonator.wait(qubit.thermalization_time * u.ns)
                        qubit_pair.align()
                    
                    if not node.parameters.use_qubit_flux_pulse:
                        for flux_q in flux_qubits:
                            flux_q.z.set_dc_offset(flux_qubit)
                        wait(1000)

                    qubit.xy.play("x180")
                    qubit.z.wait(qubit.xy.operations["x180"].length // 4 + XY_delay // 4)
                    qubit_pair.coupler.wait(qubit.xy.operations["x180"].length // 4 + XY_delay // 4)
                    for flux_q in flux_qubits:
                        flux_q.z.wait(qubit.xy.operations["x180"].length // 4 + XY_delay // 4)
                    
                    if node.parameters.use_qubit_flux_pulse:
                        for flux_q in flux_qubits:
                            assign(comp_flux_qubit, arb_flux_bias_offset[flux_q.name] + flux_qubit)
                            flux_q.z.play(
                                "const",
                                amplitude_scale=comp_flux_qubit / flux_q.z.operations["const"].amplitude,
                                duration=t,
                            )

                    qubit.z.wait(200)
                    qubit_pair.coupler.wait(200)
                    for flux_q in flux_qubits:
                        flux_q.z.wait(200)
                    qubit_pair.align()

                    # Measure the state of the resonators
                    if node.parameters.use_state_discrimination:
                        readout_state(qubit, state[i])
                        save(state[i], state_st[i])
                    else:
                        qubit.resonator.measure("readout", qua_vars=(I[i], Q[i]))
                        # save data
                        save(I[i], I_st[i])
                        save(Q[i], Q_st[i])
        # Measure sequentially
        if not node.parameters.multiplexed:
            align()

    with stream_processing():
        n_st.save("n")
        for i in range(num_qubits):
            if node.parameters.use_state_discrimination:
                state_st[i].buffer(len(idle_times)).buffer(len(fluxes_qubit)).average().save(f"state{i + 1}")
            else:
                I_st[i].buffer(len(idle_times)).buffer(len(fluxes_qubit)).average().save(f"I{i + 1}")
                Q_st[i].buffer(len(idle_times)).buffer(len(fluxes_qubit)).average().save(f"Q{i + 1}")


# %% {Simulate_or_execute}
if node.parameters.simulate:
    # Simulates the QUA program for the specified duration
    simulation_config = SimulationConfig(duration=node.parameters.simulation_duration_ns  // 4)  # In clock cycles = 4ns
    job = qmm.simulate(config, t1_vs_qubit_flux, simulation_config)
    # Get the simulated samples and plot them for all controllers
    samples = job.get_simulated_samples()
    samples.con1.plot()
    node.results = {"figure": plt.gcf()}
    wf_report = job.get_simulated_waveform_report()
    wf_report.create_plot(samples, plot=True, save_path=None)
    node.machine = machine
    node.save()

elif node.parameters.load_data_id is None:
    with qm_session(qmm, config, timeout=node.parameters.timeout) as qm:
        job = qm.execute(t1_vs_qubit_flux)
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
        ds = fetch_results_as_xarray(job.result_handles, qubits, {"idle_time": idle_times, "flux_qubit": fluxes_qubit})
        # Convert IQ data into volts
        if not node.parameters.use_state_discrimination:
            ds = convert_IQ_to_V(ds, qubits)
        # Convert time into µs
        ds = ds.assign_coords(idle_time=4 * ds.idle_time / u.us)  # convert to µs
        ds.idle_time.attrs = {"long_name": "idle time", "units": "µs"}
    else:
        load_data_id = node.parameters.load_data_id
        node = node.load_from_id(load_data_id)
        ds = node.results["ds"]
        restore_load_data_id(node, load_data_id)
        machine = node.machine
        qubits = resolve_qubits_from_node(machine, node)
        if node.parameters.qubit_flux is None or node.parameters.qubit_flux == "":
            flux_qubits = qubits
        else:
            flux_qubits = [machine.qubits[q] for q in node.parameters.qubit_flux]
        flux_qubit_names = [q.name for q in flux_qubits]

    # %% {Plotting}
    ds = ds.assign_coords(flux_mV=ds.flux_qubit * 1e3)
    flux_label = ", ".join(flux_qubit_names)

    grid = QubitGrid(ds, [q.grid_location for q in qubits])
    grid.fig.set_size_inches(12, 12 * len(qubits))

    for ax, qubit in grid_iter(grid):
        qname = qubit["qubit"]

        if node.parameters.use_state_discrimination:
            im = ds.sel(qubit=qname).state.plot(
                ax=ax, x="flux_mV", y="idle_time", add_colorbar=False,
                cmap="viridis"
            )
            ax.set_ylabel("Idle time (µs)")
        else:
            im = ds.sel(qubit=qname).I.plot(
                ax=ax, x="flux_mV", y="idle_time", add_colorbar=False,
                cmap="viridis"
            )
            ax.set_ylabel("Idle time (µs)")

        ax.set_title(f"{qname} (flux on {flux_label})")
        ax.set_xlabel(f"Qubit flux on {flux_label} (mV)")
        cb = grid.fig.colorbar(im, ax=ax)
        cb.set_label(("state" if node.parameters.use_state_discrimination else "I"))

    grid.fig.suptitle("Coupling strength check")
    plt.tight_layout()

    node.results = {
        "ds": ds,
        "figure": grid.fig,
    }
    plt.show()

    # %% {Save_results}
    if node.parameters.load_data_id is None:
        node.outcomes = {q.name: "successful" for q in qubits}
        node.results["initial_parameters"] = node.parameters.model_dump(mode="json")
        node.machine = machine
        node.save()

    

# %%
