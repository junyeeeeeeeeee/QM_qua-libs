"""
        T1 vs COUPLER FLUX STATISTICS
Repeated T1-vs-coupler-flux measurements (05b) to build statistical distributions.
For each iteration, T1 is extracted at every coupler flux point by fitting exponential decay.

Prerequisites:
    - Same as 05b_T1_vs_coulper_flux.
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
from quam_libs.lib.fit import fit_decay_exp
from qualang_tools.results import progress_counter, fetching_tool
from qualang_tools.loops import from_array
from qualang_tools.multi_user import qm_session
from qualang_tools.units import unit
from qm import SimulationConfig
from qm.qua import *
from typing import Literal, Optional, List
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from time import time

# %% {Node_parameters}
class Parameters(NodeParameters):
    qubits: Optional[List[str]] = ["q4", "q5"]
    qubit_pair: str = "coupler_q4_q5"
    num_averages: int = 200
    min_wait_time_in_ns: int = 16
    max_wait_time_in_ns: int = 100000
    wait_time_step_in_ns: int = 1000
    flux_point_joint_or_independent_or_arbitrary: Literal["joint", "independent", "arbitrary"] = "joint"
    reset_type: Literal["active", "thermal"] = "active"
    use_state_discrimination: bool = True
    simulate: bool = False
    simulation_duration_ns: int = 10000
    timeout: int = 100
    load_data_id: Optional[int] = None
    multiplexed: bool = True
    reset_coupler_bias: bool = False
    coupler_flux_min: float = -0.8
    coupler_flux_max: float = -0.2
    coupler_flux_num_points: int = 51
    use_coupler_flux_pulse: bool = False
    histo_num: int = 10

node = QualibrationNode(name="05bst_T1_vs_coupler_flux_statics", parameters=Parameters())


# %% {Initialize_QuAM_and_QOP}
u = unit(coerce_to_integer=True)
machine = QuAM.load()
config = machine.generate_config()
if node.parameters.load_data_id is None:
    qmm = machine.connect()

qubit_pair = machine.qubit_pairs[node.parameters.qubit_pair]

if node.parameters.qubits is None or node.parameters.qubits == "":
    qubits = qubit_pair.qubit_control
else:
    qubits = [machine.qubits[q] for q in node.parameters.qubits]
num_qubits = len(qubits)


# %% {QUA_program}
n_avg = node.parameters.num_averages
idle_times = np.arange(
    node.parameters.min_wait_time_in_ns // 4,
    node.parameters.max_wait_time_in_ns // 4,
    node.parameters.wait_time_step_in_ns // 4,
)

fluxes_coupler = np.linspace(
    node.parameters.coupler_flux_min,
    node.parameters.coupler_flux_max,
    node.parameters.coupler_flux_num_points,
)
reset_coupler_bias = node.parameters.reset_coupler_bias
flux_point = node.parameters.flux_point_joint_or_independent_or_arbitrary

if flux_point == "arbitrary":
    arb_flux_bias_offset = {q.name: q.z.arbitrary_offset for q in qubits}
else:
    arb_flux_bias_offset = {q.name: 0.0 for q in qubits}

with program() as t1_vs_coupler_flux:
    flux_coupler = declare(float)
    comp_flux_qubit = declare(float)
    I, I_st, Q, Q_st, n, n_st = qua_declaration(num_qubits=num_qubits)
    t = declare(int)
    if node.parameters.use_state_discrimination:
        state = [declare(int) for _ in range(num_qubits)]
        state_st = [declare_stream() for _ in range(num_qubits)]

    for i, qubit in enumerate(qubits):
        XY_delay = qubit.xy.opx_output.delay + 4
        machine.set_all_fluxes(flux_point=flux_point, target=qubit)
        if "c" in qubit.id:
            qubit.z.set_dc_offset(qubit.z.joint_offset)
        qubit.z.settle()
        qubit.align()

        if reset_coupler_bias:
            qubit_pair.coupler.set_dc_offset(0.0)
        else:
            qubit_pair.coupler.to_decouple_idle()
        wait(1000)

        with for_(n, 0, n < n_avg, n + 1):
            save(n, n_st)
            with for_(*from_array(flux_coupler, fluxes_coupler)):
                with for_(*from_array(t, idle_times)):
                    if node.parameters.reset_type == "active":
                        active_reset_simple(qubit, "readout")
                        qubit_pair.align()
                    else:
                        qubit.resonator.wait(qubit.thermalization_time * u.ns)
                        qubit_pair.align()

                    if "coupler_qubit_crosstalk" in qubit_pair.extras:
                        assign(
                            comp_flux_qubit,
                            arb_flux_bias_offset[qubit.name]
                            + qubit_pair.extras["coupler_qubit_crosstalk"] * flux_coupler,
                        )
                    else:
                        assign(comp_flux_qubit, arb_flux_bias_offset[qubit.name])

                    if not node.parameters.use_coupler_flux_pulse:
                        qubit_pair.coupler.set_dc_offset(flux_coupler)
                        wait(1000)

                    qubit.xy.play("x180")
                    qubit.z.wait(qubit.xy.operations["x180"].length // 4 + XY_delay // 4)
                    qubit_pair.coupler.wait(qubit.xy.operations["x180"].length // 4 + XY_delay // 4)

                    qubit.z.play(
                        "const",
                        amplitude_scale=comp_flux_qubit / qubit.z.operations["const"].amplitude,
                        duration=t,
                    )
                    if node.parameters.use_coupler_flux_pulse:
                        qubit_pair.coupler.play(
                            "const",
                            amplitude_scale=flux_coupler / qubit_pair.coupler.operations["const"].amplitude,
                            duration=t,
                        )

                    qubit.z.wait(20)
                    qubit_pair.coupler.wait(20)
                    qubit_pair.align()

                    if node.parameters.use_state_discrimination:
                        readout_state(qubit, state[i])
                        save(state[i], state_st[i])
                    else:
                        qubit.resonator.measure("readout", qua_vars=(I[i], Q[i]))
                        save(I[i], I_st[i])
                        save(Q[i], Q_st[i])

        if not node.parameters.multiplexed:
            align()

    with stream_processing():
        n_st.save("n")
        for i in range(num_qubits):
            if node.parameters.use_state_discrimination:
                state_st[i].buffer(len(idle_times)).buffer(len(fluxes_coupler)).average().save(f"state{i + 1}")
            else:
                I_st[i].buffer(len(idle_times)).buffer(len(fluxes_coupler)).average().save(f"I{i + 1}")
                Q_st[i].buffer(len(idle_times)).buffer(len(fluxes_coupler)).average().save(f"Q{i + 1}")


# %% {Simulate_or_execute}
if node.parameters.simulate:
    simulation_config = SimulationConfig(duration=node.parameters.simulation_duration_ns // 4)
    job = qmm.simulate(config, t1_vs_coupler_flux, simulation_config)
    samples = job.get_simulated_samples()
    samples.con1.plot()
    node.results = {"figure": plt.gcf()}
    node.machine = machine
    node.save()
else:
    if node.parameters.load_data_id is None:
        dss = []
        start = time()

        target_counts = node.parameters.histo_num
        current_success = 0
        max_retries = target_counts + 5
        attempts = 0

        while current_success < target_counts and attempts < max_retries:
            attempts += 1
            try:
                with qm_session(qmm, config, timeout=node.parameters.timeout) as qm:
                    job = qm.execute(t1_vs_coupler_flux)
                    results = fetching_tool(job, ["n"], mode="live")
                    while results.is_processing():
                        n = results.fetch_all()[0]
                        if target_counts <= 5:
                            progress_counter(n, n_avg, start_time=results.start_time)

                ds_iter = fetch_results_as_xarray(
                    job.result_handles, qubits, {"idle_time": idle_times, "flux_coupler": fluxes_coupler}
                )
                if not node.parameters.use_state_discrimination:
                    ds_iter = convert_IQ_to_V(ds_iter, qubits)

                dss.append(ds_iter)
                current_success += 1
                print(f"Counts: {current_success} (Total attempts: {attempts})")
            except Exception as e:
                print(f"Attempt {attempts} failed: {e}. Skipping...")
                if (attempts - current_success) > 5:
                    print("Too many consecutive failures. Stopping experiment.")
                    break

        end = time()
        print(f"Total {round(end - start, 1)} sec for {current_success} counts")
        ds = xr.concat(dss, dim="iteration")
        ds = ds.assign_coords(idle_time=4 * ds.idle_time / u.us)
        ds.idle_time.attrs = {"long_name": "idle time", "units": "µs"}
        reload_qbs = False
    else:
        load_data_id = node.parameters.load_data_id
        node = node.load_from_id(load_data_id)
        ds = node.results["ds"]
        restore_load_data_id(node, load_data_id)
        machine = node.machine
        qubits = resolve_qubits_from_node(machine, node)
        reload_qbs = True


# %% {Data_analysis}
if not node.parameters.simulate:
    ds = ds.assign_coords(flux_mV=ds.flux_coupler * 1e3)
    qbs = ds.qubit.values
    iterations = ds.iteration.values
    flux_mV = ds.flux_mV.values

    if reload_qbs:
        qubits = [machine.qubits[q] for q in qbs]

    # t1_data[qubit] -> list of (n_flux,) arrays, one per iteration
    t1_data = {q: [] for q in qbs}
    t1_err_data = {q: [] for q in qbs}

    for iter_val in iterations:
        ds_iter = ds.sel(iteration=iter_val)
        if node.parameters.use_state_discrimination:
            fit_data = fit_decay_exp(ds_iter.state, "idle_time")
        else:
            fit_data = fit_decay_exp(ds_iter.I, "idle_time")

        decay = fit_data.sel(fit_vals="decay")
        decay_res = fit_data.sel(fit_vals="decay_decay")
        tau = -1 / decay
        tau_error = -tau * (np.sqrt(decay_res) / decay)

        for q_name in qbs:
            t1_vals = tau.sel(qubit=q_name).values
            t1_err_vals = tau_error.sel(qubit=q_name).values
            mask = t1_vals > 0
            t1_vals = np.where(mask, t1_vals, np.nan)
            t1_err_vals = np.where(mask, t1_err_vals, np.nan)
            t1_data[q_name].append(t1_vals)
            t1_err_data[q_name].append(t1_err_vals)

    # %% {Plotting}
    # ---- Figure 1: 2D histogram (coupler flux vs T1, color = counts) ----
    grid_mesh = QubitGrid(ds, [q.grid_location for q in qubits])
    grid_mesh.fig.set_size_inches(10, 3 * len(qubits))

    for ax, qubit_info in grid_iter(grid_mesh):
        q_name = qubit_info["qubit"]
        all_flux = []
        all_t1 = []
        for t1_vals in t1_data[q_name]:
            valid = ~np.isnan(t1_vals)
            all_flux.extend(flux_mV[valid])
            all_t1.extend(t1_vals[valid])

        all_flux = np.array(all_flux)
        all_t1 = np.array(all_t1)
        if len(all_t1) > 0:
            lower = np.percentile(all_t1, 1)
            upper = np.percentile(all_t1, 99)
            mask = (all_t1 >= lower) & (all_t1 <= upper)
            all_flux = all_flux[mask]
            all_t1 = all_t1[mask]

        if len(all_t1) > 1:
            h = ax.hist2d(
                all_flux,
                all_t1,
                bins=[len(flux_mV), 50],
                cmap="viridis",
            )
            cb = grid_mesh.fig.colorbar(h[3], ax=ax)
            cb.set_label("Counts")

        ax.set_title(q_name)
        ax.set_xlabel("Coupler flux (mV)")
        ax.set_ylabel("T1 (µs)")

    grid_mesh.fig.suptitle(f"T1 vs Coupler Flux Statistics (mesh), #={len(iterations)}")
    plt.tight_layout()
    plt.show()
    node.results = {"ds": ds}
    node.results["figure_mesh"] = grid_mesh.fig

    # ---- Figure 2: 1D T1 vs coupler flux with error bars ----
    grid_1d = QubitGrid(ds, [q.grid_location for q in qubits])
    grid_1d.fig.set_size_inches(10, 3 * len(qubits))

    for ax, qubit_info in grid_iter(grid_1d):
        q_name = qubit_info["qubit"]
        t1_stack = np.array(t1_data[q_name])  # (n_iter, n_flux)

        if node.parameters.histo_num > 1:
            t1_mean = np.nanmean(t1_stack, axis=0)
            t1_std = np.nanstd(t1_stack, axis=0)
        else:
            t1_mean = t1_stack[0]
            t1_std = np.array(t1_err_data[q_name][0])

        valid = ~np.isnan(t1_mean)
        ax.errorbar(
            flux_mV[valid],
            t1_mean[valid],
            yerr=t1_std[valid],
            fmt="o-",
            capsize=3,
        )
        ax.set_title(q_name)
        ax.set_xlabel("Coupler flux (mV)")
        ax.set_ylabel("T1 (µs)")

    grid_1d.fig.suptitle("T1 vs Coupler Flux (mean ± std)")
    plt.tight_layout()
    plt.show()
    node.results["figure_T1_coupler"] = grid_1d.fig

    stats = {}
    for q_name in qbs:
        t1_stack = np.array(t1_data[q_name])
        stats[q_name] = {
            "T1_mean_us": np.nanmean(t1_stack, axis=0).tolist(),
            "T1_std_us": np.nanstd(t1_stack, axis=0).tolist(),
            "flux_mV": flux_mV.tolist(),
        }
    node.results["t1_vs_coupler_flux_stats"] = stats

    # %% {Save_results}
    if node.parameters.load_data_id is None:
        node.results["initial_parameters"] = node.parameters.model_dump()
        node.machine = machine
        node.save()

# %%
