# %%
"""
Calibration of the Controlled-Phase (CPhase) of the EACZ Gate

Echo-Assisted CZ (EACZ) between non-adjacent qubits (e.g. q3–q5) is built from
two adjacent coupler flux pulses (cz_EACZ) played in parallel, with an echo on the bridge qubit.
Use qubit_list_index: index i maps to q{i} (target), q{i+1} (bridge), q{i+2} (control).

For each relative coupler amplitude scale, we measure:
1. The phase difference of the target qubit between control |0> and |1>
2. Optionally leakage to |f>

The optimal amplitude scale is where the phase difference is closest to π (0.5).
"""

# %% {Imports}
from qualibrate import QualibrationNode, NodeParameters
from quam_libs.components import QuAM
from quam_libs.macros import active_reset, readout_state
from quam_libs.lib.plot_utils import QubitPairGrid, grid_iter, grid_pair_names
from quam_libs.lib.save_utils import fetch_results_as_xarray, load_dataset
from qualang_tools.results import progress_counter, fetching_tool
from qualang_tools.loops import from_array
from qualang_tools.multi_user import qm_session
from qualang_tools.units import unit
from qm import SimulationConfig
from qm.qua import *
from typing import Literal, Optional, List
from types import SimpleNamespace
import matplotlib.pyplot as plt
import numpy as np
from quam_libs.lib.fit import fit_oscillation, oscillation, fix_oscillation_phi_2pi

# %% {Node_parameters}
# qubit_list_index i -> q{i} (target), q{i+1} (bridge), q{i+2} (control)
qubit_list_index = 3

def resolve_eacz_systems(machine, indexes):
    systems = []
    for idx in indexes:
        q_target = machine.qubits[f"q{idx}"]
        q_bridge = machine.qubits[f"q{idx + 1}"]
        q_control = machine.qubits[f"q{idx + 2}"]
        systems.append(
            SimpleNamespace(
                name=f"eacz_q{idx}_q{idx + 2}",
                qubit_control=q_control,
                qubit_target=q_target,
                bridge_qubit=q_bridge,
                couplers=[
                    machine.qubit_pairs[f"coupler_q{idx}_q{idx + 1}"],
                    machine.qubit_pairs[f"coupler_q{idx + 1}_q{idx + 2}"],
                ],
            )
        )
    return systems


class Parameters(NodeParameters):
    qubit_list_index: int = qubit_list_index
    num_averages: int = 2000
    flux_point_joint_or_independent: Literal["joint", "independent"] = "joint"
    reset_type: Literal["active", "thermal"] = "active"
    simulate: bool = False
    timeout: int = 100
    amp_range: float = 0.05
    amp_step: float = 0.002
    num_frames: int = 20
    load_data_id: Optional[int] = None
    plot_raw: bool = True
    measure_leak: bool = False
    coupler_operation: Literal["cz_EACZ", "cz_EACZ_slepian", "cz_EACZ_custom"] = "cz_EACZ"


node = QualibrationNode(
    name="EACZ01a_cz_phase_coupler", parameters=Parameters()
)
assert not (node.parameters.simulate and node.parameters.load_data_id is not None), (
    "If simulate is True, load_data_id must be None, and vice versa."
)

# %% {Initialize_QuAM_and_QOP}
u = unit(coerce_to_integer=True)
machine = QuAM.load()

list_indexes = [node.parameters.qubit_list_index]
eacz_systems = resolve_eacz_systems(machine, list_indexes)
num_eacz_systems = len(eacz_systems)

initial_coupler_amplitudes = {
    cp.name: cp.coupler.operations[node.parameters.coupler_operation].amplitude
    for system in eacz_systems
    for cp in system.couplers
}

config = machine.generate_config()
octave_config = machine.get_octave_config()
if node.parameters.load_data_id is None:
    qmm = machine.connect()

# %% {QUA_program}
n_avg = node.parameters.num_averages
flux_point = node.parameters.flux_point_joint_or_independent
coupler_operation = node.parameters.coupler_operation

amplitudes = np.arange(
    1 - node.parameters.amp_range,
    1 + node.parameters.amp_range,
    node.parameters.amp_step,
)
frames = np.arange(0, 1, 1 / node.parameters.num_frames)


def get_eacz_timing(coupler_pair, bridge_q, operation):
    """Clock-cycle waits compensating LF coupler vs MW XY port delay (see 05b / 67b)."""
    cz_pulse = coupler_pair.coupler.operations[operation]
    coupler_len_cc = cz_pulse.length // 4
    x180_len_cc = bridge_q.xy.operations["x180"].length // 4
    coupler_delay_cc = coupler_pair.coupler.opx_output.delay // 4
    xy_delay_cc = (bridge_q.xy.opx_output.delay + 4) // 4
    return coupler_len_cc, x180_len_cc, coupler_delay_cc, xy_delay_cc


def eacz_align(qubit_control, qubit_target, bridge_qubit, coupler_pairs):
    """Synchronize all EACZ channels (control, target, bridge, couplers)."""
    channels = [
        qubit_control.xy.name,
        qubit_control.z.name,
        qubit_control.resonator.name,
        qubit_target.xy.name,
        qubit_target.z.name,
        qubit_target.resonator.name,
        bridge_qubit.xy.name,
        bridge_qubit.z.name,
        bridge_qubit.resonator.name,
    ]
    for coupler_pair in coupler_pairs:
        channels.append(coupler_pair.coupler.name)
    align(*channels)


def play_eacz_sequence(
    coupler_pairs,
    bridge_q,
    amp_scale,
    coupler_len_cc,
    x180_len_cc,
    coupler_delay_cc,
    xy_delay_cc,
):
    coupler_pairs[0].coupler.play(coupler_operation, amplitude_scale=amp_scale)
    coupler_pairs[1].coupler.play(coupler_operation, amplitude_scale=amp_scale)
    bridge_q.xy.wait(coupler_len_cc + coupler_delay_cc - xy_delay_cc)
    bridge_q.xy.play("x180")
    coupler_pairs[0].coupler.wait(x180_len_cc + coupler_delay_cc)
    coupler_pairs[1].coupler.wait(x180_len_cc + coupler_delay_cc)


with program() as CPhase_Oscillations:
    amp = declare(fixed)
    frame = declare(fixed)
    control_initial = declare(int)
    n = declare(int)
    n_st = declare_stream()
    state_control = [declare(int) for _ in range(num_eacz_systems)]
    state_target = [declare(int) for _ in range(num_eacz_systems)]
    state_st_control = [declare_stream() for _ in range(num_eacz_systems)]
    state_st_target = [declare_stream() for _ in range(num_eacz_systems)]

    for i, system in enumerate(eacz_systems):
        qubit_control = system.qubit_control
        qubit_target = system.qubit_target
        bridge_q = system.bridge_qubit
        coupler_pairs = system.couplers
        coupler_len_cc, x180_len_cc, coupler_delay_cc, xy_delay_cc = get_eacz_timing(
            coupler_pairs[0], bridge_q, coupler_operation
        )

        if flux_point == "independent":
            machine.apply_all_flux_to_min()
        elif flux_point == "joint":
            machine.apply_all_flux_to_joint_idle()
        else:
            machine.apply_all_flux_to_zero()
        wait(1000)

        with for_(n, 0, n < n_avg, n + 1):
            save(n, n_st)
            with for_(*from_array(amp, amplitudes)):
                with for_(*from_array(frame, frames)):
                    with for_(*from_array(control_initial, [0, 1])):
                        if not node.parameters.simulate:
                            if node.parameters.reset_type == "active":
                                active_reset(qubit_control)
                                active_reset(qubit_target)
                            else:
                                wait(qubit_control.thermalization_time * u.ns)

                        eacz_align(qubit_control, qubit_target, bridge_q, coupler_pairs)

                        reset_frame(qubit_target.xy.name)
                        reset_frame(qubit_control.xy.name)

                        eacz_align(qubit_control, qubit_target, bridge_q, coupler_pairs)

                        with if_(control_initial == 1):
                            qubit_control.xy.play("x180")
                        qubit_target.xy.play("x90")

                        eacz_align(qubit_control, qubit_target, bridge_q, coupler_pairs)

                        play_eacz_sequence(
                            coupler_pairs,
                            bridge_q,
                            amp,
                            coupler_len_cc,
                            x180_len_cc,
                            coupler_delay_cc,
                            xy_delay_cc,
                        )
                        eacz_align(qubit_control, qubit_target, bridge_q, coupler_pairs)
                        play_eacz_sequence(
                            coupler_pairs,
                            bridge_q,
                            amp,
                            coupler_len_cc,
                            x180_len_cc,
                            coupler_delay_cc,
                            xy_delay_cc,
                        )
                        eacz_align(qubit_control, qubit_target, bridge_q, coupler_pairs)

                        frame_rotation_2pi(frame, qubit_target.xy.name)
                        qubit_target.xy.play("x90")

                        readout_state(qubit_target, state_target[i])
                        readout_state(qubit_control, state_control[i])
                        save(state_control[i], state_st_control[i])
                        save(state_target[i], state_st_target[i])
        align()

    with stream_processing():
        n_st.save("n")
        for i in range(num_eacz_systems):
            state_st_control[i].buffer(2).buffer(len(frames)).buffer(len(amplitudes)).buffer(n_avg).save(
                f"state_control{i + 1}"
            )
            state_st_target[i].buffer(2).buffer(len(frames)).buffer(len(amplitudes)).buffer(n_avg).save(
                f"state_target{i + 1}"
            )

# %% {Simulate_or_execute}
if node.parameters.simulate:
    simulation_config = SimulationConfig(duration=30_000 // 4)
    job = qmm.simulate(config, CPhase_Oscillations, simulation_config)
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
    node.machine = machine
    node.save()
elif node.parameters.load_data_id is None:
    with qm_session(qmm, config, timeout=node.parameters.timeout) as qm:
        job = qm.execute(CPhase_Oscillations)
        results = fetching_tool(job, ["n"], mode="live")
        while results.is_processing():
            n = results.fetch_all()[0]
            progress_counter(n, n_avg, start_time=results.start_time)

# %% {Data_fetching_and_dataset_creation}
if not node.parameters.simulate:
    if node.parameters.load_data_id is None:
        ds = fetch_results_as_xarray(
            job.result_handles,
            eacz_systems,
            {
                "control_axis": [0, 1],
                "frame": frames,
                "amp": amplitudes,
                "N": np.linspace(1, n_avg, n_avg),
            },
        )
    else:
        ds, machine = load_dataset(node.parameters.load_data_id)
        eacz_systems = resolve_eacz_systems(machine, list_indexes)

    node.results = {"ds": ds}

# %% {Data_analysis}
if not node.parameters.simulate:

    def abs_amp(system, amp):
        ref_pulse = system.couplers[0].coupler.operations[coupler_operation]
        return amp * ref_pulse.amplitude

    ds = ds.assign_coords(
        {"amp_full": (["qubit", "amp"], np.array([abs_amp(system, ds.amp) for system in eacz_systems]))}
    )

# %% Analysis
if not node.parameters.simulate:
    phase_diffs = {}
    optimal_amps = {}
    optimal_scales = {}
    fitted = {}

    for system in eacz_systems:
        ds_pair = ds.sel(qubit=system.name)
        fit_data = fit_oscillation(ds_pair.state_target.mean(dim="N"), "frame")

        ds_pair = ds_pair.assign(
            {
                "fitted": oscillation(
                    ds_pair.frame,
                    fit_data.sel(fit_vals="a"),
                    fit_data.sel(fit_vals="f"),
                    fit_data.sel(fit_vals="phi"),
                    fit_data.sel(fit_vals="offset"),
                )
            }
        )

        if node.parameters.plot_raw:
            plt.figure()
            ds_pair.mean(dim="N").to_array().sel(variable=["state_target", "fitted"]).stack(
                control_axis_fit=("control_axis", "variable")
            ).plot.line(x="frame", col="amp", col_wrap=4)
            plt.show()

        phase = fix_oscillation_phi_2pi(fit_data)
        phase_diff = (phase.sel(control_axis=0) - phase.sel(control_axis=1)) % 1

        try:
            coeffs = np.polyfit(phase_diff.amp, phase_diff, 2)
            a, b, c = coeffs
            c_shifted = c - 0.5
            discriminant = b**2 - 4 * a * c_shifted

            if discriminant >= 0:
                root1 = (-b + np.sqrt(discriminant)) / (2 * a)
                root2 = (-b - np.sqrt(discriminant)) / (2 * a)
                min_amp, max_amp = min(phase_diff.amp), max(phase_diff.amp)
                optimal_scale = root1 if min_amp <= root1 <= max_amp else root2
            else:
                optimal_scale = float(np.abs(phase_diff - 0.5).idxmin("amp"))

            fitted[system.name] = np.polyval(coeffs, phase_diff.amp)
        except Exception:
            print(f"Fitting failed for {system.name}")
            optimal_scale = float(np.abs(phase_diff - 0.5).idxmin("amp"))

        phase_diffs[system.name] = phase_diff
        optimal_scales[system.name] = optimal_scale
        optimal_amps[system.name] = optimal_scale * system.couplers[0].coupler.operations[coupler_operation].amplitude

        print(f"parameters for {system.name}: amp_scale={optimal_scales[system.name]:.4f}")

        if node.parameters.measure_leak:
            ds_selected = ds.isel(control_axis=1)
            populations = ((ds_selected.state_control == 1) & (ds_selected.state_target == 1)).mean(
                dim=["N", "frame"]
            )

# %%
if not node.parameters.simulate:
    grid_names, qubit_pair_names = grid_pair_names(eacz_systems)
    grid = QubitPairGrid(grid_names, qubit_pair_names)
    for ax, qubit_pair in grid_iter(grid):
        phase_diffs[qubit_pair["qubit"]].plot.line(ax=ax, x="amp_full")
        if qubit_pair["qubit"] in fitted:
            ax.plot(phase_diffs[qubit_pair["qubit"]].amp_full, fitted[qubit_pair["qubit"]])
        ax.plot([optimal_amps[qubit_pair["qubit"]]], [0.5], marker="o", color="red")
        ax.axhline(y=0.5, color="red", linestyle="--", lw=0.5)
        ax.axvline(x=optimal_amps[qubit_pair["qubit"]], color="red", linestyle="--", lw=0.5)
        ax.set_title(qubit_pair["qubit"])
        ax.set_xlabel("Amplitude (V)")
        ax.set_ylabel("Phase difference")

    plt.suptitle("EACZ phase calibration", y=0.95)
    plt.tight_layout()
    plt.show()
    node.results["figure_phase"] = grid.fig

    if node.parameters.measure_leak:
        grid = QubitPairGrid(grid_names, qubit_pair_names)
        for ax, qubit_pair in grid_iter(grid):
            plot_data = populations.sel(qubit=qubit_pair["qubit"])
            ax.scatter(plot_data.amp, 100 * (0.5 - plot_data.values), alpha=0.6, edgecolors="w")
            ax.set_title(f'{qubit_pair["qubit"]}')
            ax.axvline(x=optimal_amps[qubit_pair["qubit"]], color="red", linestyle="--", lw=0.5)
            ax.set_xlabel("Amplitude (amp)")
            ax.set_ylabel("Leak population [%]")
            ax.grid()
        plt.suptitle("Leak probability", y=0.95)
        plt.tight_layout()
        plt.show()
        node.results["figure_leak"] = grid.fig

# %% {Update_state}
if not node.parameters.simulate:
    if node.parameters.load_data_id is None:
        with node.record_state_updates():
            for system in eacz_systems:
                scale = optimal_scales[system.name]
                for cp in system.couplers:
                    cp.coupler.operations[coupler_operation].amplitude = (
                        scale * initial_coupler_amplitudes[cp.name]
                    )

# %% {Save_results}
if not node.parameters.simulate:
    node.outcomes = {system.name: "successful" for system in eacz_systems}
    node.results["initial_parameters"] = node.parameters.model_dump()
    node.machine = machine
    node.save()

# %%
