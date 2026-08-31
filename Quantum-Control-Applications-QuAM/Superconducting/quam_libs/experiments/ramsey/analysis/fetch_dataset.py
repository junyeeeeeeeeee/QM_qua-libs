import xarray as xr

from quam_libs.experiments.ramsey.parameters import Parameters, get_idle_times_in_clock_cycles
from quam_libs.lib.qua_datasets import convert_IQ_to_V
from quam_libs.lib.save_utils import fetch_results_as_xarray


def fetch_dataset(job, qubits, node_parameters: Parameters) -> xr.Dataset:
    """
    Fetches the measurement results for the Ramsey calibration node.

    Returns:
    - xr.Dataset: An xarray dataset containing the fetched measurement results with the following structure:

      **Coordinates:**
      - `sign` : A dimension with values `[-1, 1]`, representing the sign of the frequency detuning.
      - `time` : A dimension representing idle times in nanoseconds.
      - `qubit` : A dimension listing the names of the qubits involved in the measurement.

      **Data Variables:**
      - One or more measurement variables, e.g., `I` or `Q`, based on the stream handles fetched from the job.
        - Each variable has dimensions corresponding to the coordinates (`sign`, `time`, `qubit`).

    """
    idle_times = get_idle_times_in_clock_cycles(node_parameters)

    ds = fetch_results_as_xarray(job.result_handles, qubits, {"sign": [-1, 1], "time": idle_times})
    # State discrimination streams only ``state``; asking the voltage helper
    # for absent I/Q variables raises after an otherwise successful acquisition.
    # Convert exactly the analog quadratures that the QUA program returned and
    # leave state-only datasets untouched.
    iq_variables = [name for name in ("I", "Q") if name in ds.data_vars]
    if iq_variables:
        ds = convert_IQ_to_V(ds, qubits, iq_variables)

    ds = ds.assign_coords({"time": (["time"], 4 * idle_times)})

    ds.time.attrs["long_name"] = "idle_time"
    ds.time.attrs["units"] = "ns"

    return ds
