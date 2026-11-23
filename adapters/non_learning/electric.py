"""EVRPTW-D Parquet wiring for the group's pinned distance/road search engines."""

import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np

from ..paths import EV_DEPS as EV

VNS = EV / "vns_ts"
VNS_SEARCH_ARGS = ("--granular", "30", "--init-time-check", "0")
BRIDGE = EV / "benchmark" / "Other_benchmark"
BRIDGE_SHA = "bd94474c551b39ac90e98a25a2cc43cb919f81b8"
UPSTREAM = {
    "vns-ts": "7e646c0062794a26184dfb55ac00840a49879ce8",
    "routingblocks": "2d2ee06c79b5d458b1aeeafd95a2b42b5441079a",
    "hma": "0ebbcaa4c2b8385c2c9bff06d60689cb2c64229d",
}
sys.path.insert(0, str(EV / "python"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inputs(task):
    if task.info["problem_type"] != "evrptw":
        raise ValueError("An EVRPTW view is required")
    v = json.loads(task.info["vehicle_json"])
    if v["initial_soc"] != 1.0 or v["charging"]["charge_policy"] != "full":
        raise ValueError("These road adapters require the release's initially full battery and full charging")
    rows = task.nodes.to_pylist()
    n = task.info["customer_count"]
    m = task.info["station_count"]
    if [r["type"] for r in rows] != ["depot"] + ["customer"] * n + ["charging"] * m:
        raise ValueError("Unexpected terminal order")
    if [r["node_idx"] for r in rows] != list(range(len(rows))):
        raise ValueError("Noncanonical node indexes")
    # Native evaluators work in kWh above reserve. They recharge the same missing
    # kWh as the physical battery and require a nonnegative balance on arrival.
    full = float(v["battery_capacity_kwh"])
    reserve = full * v["reserve_soc"]
    km = np.asarray(task.matrices["shortest_distance_m"], dtype=np.float64) / 1000.0
    matrices = dict(D_km=km, T_s=np.asarray(task.matrices["shortest_distance_time_s"], dtype=np.float64),
                    E_kwh=km * v["specific_energy_kwh_per_km"])
    customers = rows[1:n + 1]
    start, end = v["operating_window_s"]
    cfg = dict(instance_id=task.info["instance_id"], num_customers=n, num_charging_stations=m,
               battery_capacity_kwh=full - reserve, reserve_kwh=reserve,
               cargo_capacity_cm3=v["cargo_capacity_cm3"], working_start_s=start, working_end_s=end,
               vehicle_fixed_cost_usd=0.0, distance_unit_cost_usd_per_km=1.0,
               coordinates=[[r["lon"], r["lat"]] for r in rows],
               demands_cm3=[r["demand_cm3"] for r in customers],
               service_time_s=[r["service_s"] for r in customers],
               tw_s=[[r["ready_s"], r["due_s"]] for r in customers],
               effective_charging_power_kw=[r["charge_kw"] for r in rows[n + 1:]])
    return cfg, matrices


def _routingblocks(cfg, matrices):
    api = _load("evb_road_routingblocks", BRIDGE / "routingblocks_adapter" / "run.py")
    return api, api.create_instance(cfg, matrices)


def _run_routingblocks(cfg, matrices, ctx):
    api, (py, cpp, evaluation) = _routingblocks(cfg, matrices)
    params = api.ALNSParams(**json.loads((api.VENDOR / "examples/evrptw/config.json").read_text()))
    best = math.inf

    def observed(solution, stage, iteration):
        nonlocal best
        if not solution.feasible or solution.cost >= best:
            return
        routes = [[x.vertex_id for x in route] for route in solution
                  if any(x.vertex.is_customer for x in route)]
        if ctx.emit(routes, stage=stage, iteration=iteration, native_distance_km=solution.cost):
            best = solution.cost

    until = time.monotonic() + ctx.remaining()
    solver = api.WeightedALNS(evaluation, py, cpp, params, ctx.seed, cfg,
                              ctx.started, until, observed)
    try:
        solver.run(ctx.remaining(), sys.maxsize, sys.maxsize)
    except RuntimeError as error:
        if api.EXPIRED not in str(error):
            raise
    ctx.info(iterations=solver._iters, termination=solver.stop_reason or "time_limit")


def _native_input(path, cfg, matrices):
    # Same explicit native file grammar as hma_adapter.run.export_native. The
    # release supplies float64 distance-path arrays, each serialized round-trip.
    n, m = cfg["num_customers"], cfg["num_charging_stations"]
    size = n + m + 1
    start, end = cfg["working_start_s"], cfg["working_end_s"]
    with path.open("w") as f:
        f.write(f"NAME : {cfg['instance_id']}\nTYPE : EVRPTW-ICLR-REFERENCE\nDIMENSION : {size}\n")
        for key, value in dict(VEHICLES=n + 1, DISPATCHINGCOST=0, UNITCOST=1,
                               CAPACITY=cfg["cargo_capacity_cm3"], ELECTRIC_POWER=cfg["battery_capacity_kwh"],
                               CONSUMPTION_RATE=1, RECHARGING_RATE=1).items():
            f.write(f"{key} : {value:.17g}\n")
        f.write("EDGE_WEIGHT_TYPE : EXPLICIT\nNODE_SECTION\nID,type,x,y,delivery,pickup,ready_time,due_date,service_time\n")
        for i, (x, y) in enumerate(cfg["coordinates"]):
            kind = "d" if i == 0 else "c" if i <= n else "f"
            k = i - 1
            if kind == "c":
                ready, due = cfg["tw_s"][k]
                demand, service = cfg["demands_cm3"][k], cfg["service_time_s"][k]
            else:
                ready, due, demand, service = start, end, 0., 0.
            values = [x, y, demand, 0., ready, due, service]
            f.write(f"{i},{kind}," + ",".join(format(v, ".17g") for v in values) + "\n")
        f.write("DISTANCETIME_SECTION\nID,from_node,to_node,distance,spend_tm,energy_kwh\n")
        d, t, e = (matrices[k] for k in ("D_km", "T_s", "E_kwh"))
        for i in range(size):
            for j in range(size):
                f.write(f"{i*size+j},{i},{j},{d[i,j]:.17g},{t[i,j]:.17g},{e[i,j]:.17g}\n")
        f.write("STATION_CHARGING_SECONDS_PER_KWH\n")
        for j, power in enumerate(cfg["effective_charging_power_kw"], n + 1):
            f.write(f"{j},{3600.0/power:.17g}\n")
        f.write("DEPOT_SECTION\n0\n")


def _run_native(command, journal, log_path, ctx, env=None):
    with journal.open("x+") as stream, log_path.open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
        try:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if line.endswith("\n"):
                    event = json.loads(line)
                    meta = dict(native_distance_km=event["internal_objective"], stage=event["stage"],
                                native_elapsed_s=event["elapsed_s"])
                    if "iteration" in event:
                        meta["iteration"] = event["iteration"]
                    ctx.emit(event["routes"], **meta)
                    continue
                stream.seek(offset)
                if process.poll() is not None:
                    # Events are flushed by the native process before it exits.
                    tail = stream.readline()
                    if not tail:
                        break
                    if not tail.endswith("\n"):
                        raise RuntimeError(f"Incomplete candidate event in {journal}")
                    stream.seek(offset)
                time.sleep(0.005)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait()
    if process.returncode:
        raise RuntimeError(f"{command[0]} returned {process.returncode}, see {log_path}")


def _run_vns_ts(cfg, matrices, ctx):
    path = ctx.out / "vns-ts-input.txt"
    journal = ctx.out / "vns-ts-events.jsonl"
    log = ctx.out / "vns-ts.log"
    _native_input(path, cfg, matrices)
    remaining = ctx.remaining()
    if remaining <= 0:
        ctx.info(termination="deadline_before_launch", runs=0, vns_iterations=0, ts_iterations=0, evaluations=0)
        return
    deadline = time.monotonic() + remaining
    command = [str(VNS), str(path), *VNS_SEARCH_ARGS, "--events", str(journal), "--seed", str(ctx.seed),
               "--deadline-monotonic", str(deadline)]
    _run_native(command, journal, log, ctx)
    summary = json.loads(log.read_text())
    ctx.info(termination="time_limit", runs=summary["runs"], vns_iterations=summary["vns_iterations"],
             ts_iterations=summary["ts_iterations"], evaluations=summary["evaluations"],
             native_best_distance_km=summary["best_distance_km"], native_wall_s=summary["wall_s"])


def _run_hma(cfg, matrices, ctx):
    origin = _load("evb_road_hma", BRIDGE / "hma_adapter" / "run.py")
    path = ctx.out / "hma-input.txt"
    _native_input(path, cfg, matrices)
    budget = ctx.elapsed() + ctx.remaining()
    command = origin.make_command(path, ctx.out, cfg["num_customers"], ctx.seed, budget)
    journal = ctx.out / "hma-events.jsonl"
    stop = ctx.out / "hma-stop.json"
    env = dict(os.environ, HMA_START_MONOTONIC=str(ctx.started), HMA_BUDGET_S=str(budget),
               HMA_EVENTS_PATH=str(journal), HMA_STOP_PATH=str(stop), HMA_ACK_PATH="")
    _run_native(command, journal, ctx.out / "hma.log", ctx, env)
    if stop.exists():
        ctx.info(native_termination=json.loads(stop.read_text()))


def solve(method, task, ctx):
    provenance = ({"source_file": "adapters/vns_ts_cpp/vns_ts.cpp"}
                  if method == "vns-ts" else {"road_adapter_commit": BRIDGE_SHA})
    ctx.info(version=UPSTREAM[method], **provenance,
             objective="directed shortest-path km, zero vehicle fixed cost", matrix_precision="float64",
             resource_units="seconds, cm3, usable kWh above reserve, effective station kW")
    cfg, matrices = _inputs(task)
    if method == "vns-ts":
        _run_vns_ts(cfg, matrices, ctx)
    elif method == "routingblocks":
        _run_routingblocks(cfg, matrices, ctx)
    elif method == "hma":
        _run_hma(cfg, matrices, ctx)
    else:
        raise ValueError(method)


def charge_probe(method, task, from_idx, station_idx, arrival_kwh):
    cfg, matrices = _inputs(task)
    n, reserve = cfg["num_customers"], cfg["reserve_kwh"]
    full = cfg["battery_capacity_kwh"] + reserve
    if not n < station_idx < len(cfg["coordinates"]):
        raise ValueError("Probe must terminate at a release charging station")
    if from_idx != 0:
        # Choose the supplied origin as the start of the probe only. Its arcs
        # to/from the actual charging station remain the original directed arcs.
        order = np.arange(len(cfg["coordinates"]))
        order[0], order[from_idx] = from_idx, 0
        matrices = {k: v[np.ix_(order, order)] for k, v in matrices.items()}
    energy = matrices["E_kwh"][0, station_idx]
    if not math.isclose(full - energy, arrival_kwh, abs_tol=1e-7):
        raise ValueError("The probe arrival must correspond to the full departure on the supplied arc")
    if method == "vns-ts":
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "probe.txt"
            _native_input(path, cfg, matrices)
            output = subprocess.check_output([str(VNS), str(path), *VNS_SEARCH_ARGS, "--probe", str(station_idx)], text=True)
        energy, charge_s, usable = map(float, output.split())
        departure = usable + reserve
        source = "C++ VNS/TS --probe: directed arc energy, full charge time and station departure battery"
    elif method == "routingblocks":
        _, (_, cpp, evaluation) = _routingblocks(cfg, matrices)
        replay = evaluation.replay(cpp, [0, station_idx, 0])
        power = cfg["effective_charging_power_kw"][station_idx - n - 1]
        charge_s = replay[7]
        energy = charge_s * power / 3600.
        departure = replay[6] + matrices["E_kwh"][station_idx, 0] + reserve
        source = "FullRechargeEvaluation.replay: accumulated charging seconds and post-return battery"
    elif method == "hma":
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path = folder / "probe.txt"
            _native_input(path, cfg, matrices)
            output = folder / "states.txt"
            subprocess.run([str(EV / "hma_probe"), str(path), str(station_idx), str(output)],
                           stdout=subprocess.DEVNULL, check=True)
            energy, charge_s, usable = map(float, output.read_text().split())
            departure = usable + reserve
        source = "HMA reference_route_status: native origin departure and station arrival/departure states"
    else:
        raise ValueError(method)
    return dict(energy_kwh=energy, charge_s=charge_s, departure_kwh=departure, source=source)
