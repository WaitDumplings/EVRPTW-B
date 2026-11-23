"""LKH-3.0.10 on the directed benchmark matrices, with independent VRPTW travel times."""

import json
import math
from pathlib import Path
import subprocess
import tempfile
import time

import numpy as np

from ..paths import DEPS


INT_MAX = 2**31 - 1
FORBIDDEN_COST = INT_MAX // 2 // 100  # M at the upstream default PRECISION
EXACT_LIMIT = 2**53 - 1  # double window/service fields mixed with the int64 clock
LKH_BIN = DEPS / "lkh-3.0.10" / "bin"


def _nonnegative(values, label):
    a = np.asarray(values, dtype=np.float64)
    if not a.size or not np.isfinite(a).all() or (a < 0).any():
        raise ValueError(f"{label} must contain finite nonnegative values")
    return a


def _matrix(task, name, dimension):
    a = _nonnegative(task.matrices[name], name)
    if a.shape != (dimension, dimension) or np.any(a.diagonal() != 0):
        raise ValueError(f"{name} must be {dimension} by {dimension} with a zero diagonal")
    return a


def _packing(demand, capacity):
    """First-fit decreasing on the same integer loads LKH receives."""
    remaining = []
    for load in sorted(demand, reverse=True):
        if load > capacity:
            raise ValueError(f"customer demand {load} exceeds vehicle capacity {capacity} after rounding")
        for i, space in enumerate(remaining):
            if load <= space:
                remaining[i] -= load
                break
        else:
            remaining.append(capacity - load)
    return len(remaining)


def _loads(task, cols):
    raw = _nonnegative(cols["demand_cm3"], "demand")
    capacity = float(task.info["capacity_cm3"])
    if not math.isfinite(capacity) or capacity <= 0 or raw[0] != 0:
        raise ValueError("expected positive vehicle capacity and zero depot demand")
    # Start at the classical adapters' 1000 ticks/cm³; reduce uniformly for int32.
    factor = 1000.0
    while max(float(raw.max()), capacity) * factor > INT_MAX:
        factor /= 10
    rounded_capacity = math.floor(capacity * factor)
    if rounded_capacity <= 0:
        raise ValueError("rounded LKH vehicle capacity is zero")
    demand = np.ceil(raw * factor).astype(np.int64).tolist()
    return demand, rounded_capacity, factor


def _times(task, cols, demand, capacity, vehicles):
    size = len(demand)
    n = size - 1
    travel = _matrix(task, "shortest_distance_time_s", size)
    service = _nonnegative(cols["service_s"], "service time")
    early = _nonnegative(cols["ready_s"], "window start").copy()
    late = _nonnegative(cols["due_s"], "window end").copy()
    early[0], late[0] = _nonnegative(json.loads(task.info["vehicle_json"])["operating_window_s"],
                                    "depot operating window")
    if service[0] != 0:
        raise ValueError("LKH requires zero depot service time")
    max_travel = float(travel.max())
    max_raw = max(max_travel, float(service.max()), float(early.max()), float(late.max()))
    # Conservative bounds for all intermediate tours, including infeasible ones.
    load_penalty = n * (sum(demand) + capacity)
    size_penalty = vehicles * (n + 2 * (n + vehicles))
    constant_penalty = load_penalty + size_penalty
    if constant_penalty + (n + vehicles) * size * FORBIDDEN_COST >= EXACT_LIMIT:
        raise ValueError("LKH cumulative penalty exceeds the exact clock range even at the coarsest time scale")
    factor = 1000.0
    while True:
        if max_raw * factor > INT_MAX:
            factor /= 10
            continue
        s = np.ceil(service * factor).astype(np.int64)
        a = np.ceil(early * factor).astype(np.int64)
        b = np.floor(late * factor).astype(np.int64)
        inverted = np.flatnonzero(a > b)
        if inverted.size:
            i = int(inverted[0])
            raise ValueError(f"inward rounding makes window empty at node {i}: "
                             f"[{a[i]}, {b[i]}] at {factor:g} ticks/second")
        latest = max(int(a.max()), int(b.max()))
        effective_arc = max(FORBIDDEN_COST, math.ceil(max_travel * factor))
        clock = latest + sum(map(int, s)) + size * effective_arc
        penalty = (n + vehicles) * (clock + latest) + constant_penalty
        if penalty <= EXACT_LIMIT:
            return (np.ceil(travel * factor).astype(np.int32), s, a, b), dict(
                time_ticks_per_second=factor, seconds_per_time_tick=1.0 / factor,
                max_effective_arc_ticks=effective_arc, clock_upper_bound_ticks=clock,
                penalty_upper_bound=penalty, exact_integer_limit=EXACT_LIMIT)
        factor /= 10


def _problem(task, destination):
    cols = task.nodes.to_pydict()
    d = len(cols["node_idx"])
    n = d - 1
    if d < 3 or cols["node_idx"] != list(range(d)) or cols["type"] != ["depot"] + ["customer"] * n:
        raise ValueError("LKH expects a depot at index 0 followed by at least two indexed customers")
    meters = _matrix(task, "shortest_distance_m", d)
    if float(meters.max()) * 10 >= FORBIDDEN_COST - 0.5:
        raise ValueError(f"rounded decimetre edge cost reaches the LKH sentinel {FORBIDDEN_COST}")
    distance = np.rint(meters * 10).astype(np.int32)
    demand, capacity, factor = _loads(task, cols)
    boxes = _packing(demand[1:], capacity)
    vehicles = boxes + 2
    timed = task.info["problem_type"] == "vrptw"
    times, time_info = _times(task, cols, demand, capacity, vehicles) if timed else (None, {})
    with destination.open("x", encoding="ascii") as stream:
        stream.write(f"NAME: EVRPTW-B\nTYPE: {'CVRPTW' if timed else 'ACVRP'}\nDIMENSION: {d}\n"
                     f"CAPACITY: {capacity}\nVEHICLES: {vehicles}\n"
                     "EDGE_WEIGHT_TYPE: EXPLICIT\nEDGE_WEIGHT_FORMAT: FULL_MATRIX\nEDGE_WEIGHT_SECTION\n")
        np.savetxt(stream, distance, fmt="%d")
        stream.write("DEMAND_SECTION\n")
        for i, load in enumerate(demand, start=1):
            stream.write(f"{i} {load}\n")
        stream.write("DEPOT_SECTION\n1\n-1\n")
        if timed:
            travel, service, early, late = times
            stream.write("TRAVEL_TIME_SECTION\n")
            np.savetxt(stream, travel, fmt="%d")
            stream.write("SERVICE_TIME_SECTION\n")
            for i, value in enumerate(service, start=1):
                stream.write(f"{i} {value}\n")
            stream.write("TIME_WINDOW_SECTION\n")
            for i, (start, end) in enumerate(zip(early, late), start=1):
                stream.write(f"{i} {start} {end}\n")
        stream.write("EOF\n")
    return dict(distance_units="round(m * 10), decimetres", demand_ticks_per_cm3=factor,
                demand_rounding="ceil demand, floor capacity", packing="first-fit decreasing",
                packing_vehicles=boxes, vehicle_upper_bound=vehicles, fixed_vehicle_cost=0,
                internal_nodes=2 * (n + vehicles), **time_info)


def _routes(content, dimension, vehicles):
    lines = content.splitlines()
    start = lines.index("TOUR_SECTION")
    if lines[-2:] != ["-1", "EOF"]:
        raise ValueError("LKH tour lacks its terminator")
    tour = list(map(int, lines[start + 1:-2]))
    expected = dimension + vehicles - 1
    if (tour[0:1] != [1] or len(tour) != expected
            or set(tour) != set(range(1, expected + 1))):
        raise ValueError("invalid depot or node permutation in LKH tour")
    routes, route = [], [0]
    for node in tour[1:]:
        if node > dimension:
            if len(route) > 1:
                routes.append([*route, 0])
                route = [0]
        else:
            route.append(node - 1)
    if len(route) > 1:
        routes.append([*route, 0])
    return routes


def _watch(directory, dimension, vehicles, ctx, process):
    seen = set()
    seen_routes = set()

    def drain(final=False):
        pending = [p for p in directory.glob("*.tour") if p not in seen]
        # For equal file timestamps, the larger objective was observed first.
        for path in sorted(pending, key=lambda p: (p.stat().st_mtime_ns,
                                                   -int(p.stem.split("-")[1]))):
            content = path.read_text(encoding="ascii")
            if not content.endswith("-1\nEOF\n"):
                if final:
                    raise ValueError(f"incomplete LKH tour at normal exit: {path.name}")
                continue
            source, cost = path.stem.split("-")
            routes = _routes(content, dimension, vehicles)
            identity = tuple(tuple(r) for r in routes)
            if identity not in seen_routes:
                ctx.emit(routes, objective_dm=int(cost), native_tour=source)
                seen_routes.add(identity)
            seen.add(path)

    while process.poll() is None:
        drain()
        time.sleep(0.01)
    drain(final=process.returncode == 0)


def solve(method, task, ctx):
    view = task.info["problem_type"]
    if method != "lkh" or view not in ("cvrp", "vrptw"):
        raise ValueError(f"Unsupported LKH method/view: {method}/{view}")
    # prepare_lkh publishes both binaries and their receipt as one generation.
    # Resolve its symlink once, so a concurrent rebuild cannot change this run.
    generation = LKH_BIN.resolve(strict=True)
    binary = generation / ("LKH-vrptw-time" if view == "vrptw" else "LKH")
    if not binary.is_file():
        raise FileNotFoundError(f"missing prepared LKH executable: {binary}")
    record = json.loads((generation / "preparation.json").read_text(encoding="utf-8"))
    provenance = dict(archive_sha256=record["inputs"]["archive_sha256"],
                      binary_sha256=record["binaries_sha256"][binary.name])
    if view == "vrptw":
        provenance.update(source_grade="B", patch_sha256=record["inputs"]["patch_sha256"])
    if not 0 <= ctx.seed <= 2**32 - 1:
        raise ValueError("LKH SEED must fit an unsigned 32-bit integer")
    with tempfile.TemporaryDirectory(dir=ctx.out, prefix="lkh-work-") as temporary:
        directory = Path(temporary)
        meta = _problem(task, directory / "problem.vrp")
        remaining = ctx.remaining()
        params = dict(PROBLEM_FILE="problem.vrp", RUNS=1_000_000, SEED=ctx.seed,
                      MTSP_MIN_SIZE=0, TOTAL_TIME_LIMIT=format(remaining, ".9f"),
                      OUTPUT_TOUR_FILE="trial-$.tour", TOUR_FILE="run-$.tour")
        ctx.info(version="3.0.10", executable=str(binary), parameters=params,
                 native_log="lkh.log", **provenance, **meta)
        if remaining <= 0:
            return
        (directory / "solver.par").write_text(
            "".join(f"{key} = {value}\n" for key, value in params.items()), encoding="ascii")
        # The small native time/timeout processes avoid counting the Python worker's
        # pre-exec high-water mark as solver memory. --foreground retains its process group.
        wall = ctx.remaining()
        if wall <= 0:
            return
        command = ["/usr/bin/time", "-q", "-f", "%M", "-o", "peak-kib",
                   "timeout", "--foreground", "--kill-after=0.1", f"{wall:.9f}s",
                   str(binary), "solver.par"]
        with (ctx.out / "lkh.log").open("wb") as log, subprocess.Popen(
                command, cwd=directory, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT) as process:
            _watch(directory, task.nodes.num_rows, meta["vehicle_upper_bound"], ctx, process)
        timed_out = process.returncode == 124
        peak = int((directory / "peak-kib").read_text(encoding="ascii").strip())
        ctx.info(native_peak_rss_kib=peak, peak_rss_scope="GNU time: timeout and LKH, maximum in KiB",
                 native_returncode=process.returncode, stopped_at_wall_budget=timed_out)
        if process.returncode != 0 and not timed_out:
            raise RuntimeError(f"LKH exited with code {process.returncode}; see {ctx.out / 'lkh.log'}")
