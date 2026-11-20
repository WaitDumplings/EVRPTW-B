"""Feed the native double-precision, directed HGS-CVRP matrix interface."""

import ctypes as ct
import numpy as np

from ..build_hgs import REV
from ..paths import DEPS


def solve_hgs(task, ctx):
    lib = ct.CDLL(str(DEPS / "hgs-build/libhgs_observed.so"))
    array = np.ctypeslib.ndpointer(dtype=np.float64, flags="C_CONTIGUOUS")
    observer = ct.CFUNCTYPE(None, ct.POINTER(ct.c_int), ct.c_int)
    lib.hgs_run_matrix.argtypes = [ct.c_int, array, array, array, array, array,
                                   ct.c_double, ct.c_int, ct.c_double, observer]
    lib.hgs_run_matrix.restype = ct.c_int
    cols = task.nodes.to_pydict()
    n = task.nodes.num_rows
    # Positive rescaling keeps values within the upstream numerical thresholds.
    distance = np.ascontiguousarray(task.matrices["shortest_distance_m"] / 1000, dtype=np.float64)
    demand = np.ascontiguousarray(np.asarray(cols["demand_cm3"]) / 1000, dtype=np.float64)
    errors = []

    @observer
    def incumbent(sequence, length):
        try:
            routes, route = [], [0]
            for node in sequence[:length]:
                route.append(node)
                if node == 0:
                    routes.append(route)
                    route = [0]
            ctx.emit(routes)
        except Exception as exc:
            # ctypes callbacks cannot propagate Python exceptions through C++.
            errors.append(exc)

    ctx.info(version=REV, algorithm="HGS-CVRP solve_cvrp_dist_mtx", distance_units="unrounded km",
             demand_units="unrounded litres", vehicle_upper_bound=n - 1, fixed_vehicle_cost=0)
    time_left = ctx.remaining()
    if time_left > 0:
        num_routes = lib.hgs_run_matrix(
            n, np.array(cols["lon"], dtype=float), np.array(cols["lat"], dtype=float),
            distance, np.zeros(n), demand, task.info["capacity_cm3"] / 1000,
            ctx.seed, time_left, incumbent,
        )
        if errors:
            raise errors[0]
        if num_routes < 0:
            raise RuntimeError("HGS-CVRP rejected the instance; see its native diagnostic")
