"""Three single-threaded Gurobi arc-flow views, with upstream EV model and route decoder."""

import json
import math
import sys

import gurobipy as gp
import numpy as np
from gurobipy import GRB

from ..paths import EV_DEPS

GROUP_COMMIT = "09ffe9dba09f9c21c70be3590e721b0a605ffec4"
GROUP_ROOT = EV_DEPS / "group"
sys.path.insert(0, str(GROUP_ROOT / "EVRPTW_Core"))
sys.path.insert(0, str(GROUP_ROOT / "EVRPTW_Benchmark/Exact/Gurobi_Solver"))
from evrptw_core.schema import EVRPTWInstance
from gurobi_solver import GurobiEVRPTWSolver, GurobiSolverConfig, NodeMap


def _arc_energy(distance_m, vehicle):
    return (distance_m / 1000.0) * vehicle["specific_energy_kwh_per_km"]


def _charging_resources(task):
    v = json.loads(task.info["vehicle_json"])
    reserve = v["battery_capacity_kwh"] * v["reserve_soc"]
    power = task.nodes["charge_kw"].to_numpy()[1 + task.info["customer_count"]:]
    return v, reserve, power


def charge_probe(method, task, from_idx, station_idx, arrival_kwh):
    """Evaluate actual active rows of the Gurobi EV model without optimizing it."""
    v, reserve, _ = _charging_resources(task)
    solver = GurobiEVRPTWSolver(GurobiSolverConfig(time_limit_s=None, cs_copies=2, threads=1, output_flag=0))
    model, mapped, arcs = _ev_model(task, solver)
    with model:
        model.update()
        origin = mapped.solver_to_terminal.index(from_idx)
        station = mapped.solver_to_terminal.index(station_idx)
        successor = next(j for j in mapped.customer_nodes if (station, j) in arcs)
        incoming = arcs[origin, station]
        outgoing = arcs[station, successor]

        def at_active_arc(names, unknown, selected_arc, fixed):
            # At visit=1, the paired lower/upper inequalities must coincide.
            # Every arc entering the station except the selected one is zero.
            answers = []
            for name in names:
                row = model.getConstrByName(name)
                expr = model.getRow(row)
                rhs = row.RHS
                coeff = model.getCoeff(row, unknown)
                for idx in range(expr.size()):
                    var = expr.getVar(idx)
                    if var.sameAs(unknown):
                        continue
                    if var.VType == GRB.BINARY:
                        value = float(var.sameAs(selected_arc))
                    else:
                        value = fixed[var.VarName]
                    rhs -= expr.getCoeff(idx) * value
                answers.append(rhs / coeff)
            if not math.isclose(*answers, rel_tol=1e-10, abs_tol=1e-7):
                raise ValueError(f"Gurobi active rows disagree: {names}, {answers}")
            return answers[0]

        def battery_rows(i, j):
            return [f"battery_lower_{i}_{j}", f"battery_upper_{i}_{j}"]

        start_battery = model.getConstrByName("start_battery")
        start_var = model.getVarByName(f"battery[{mapped.start_depot}]")
        departure = start_battery.RHS / model.getCoeff(start_battery, start_var)
        station_battery = model.getVarByName(f"battery[{station}]")
        model_arrival = at_active_arc(battery_rows(origin, station), station_battery, incoming,
                                      {f"battery[{origin}]": departure})
        arrival_at_station = {station_battery.VarName: arrival_kwh - reserve}
        charge = at_active_arc([f"charge_time_lower_{station}", f"charge_time_upper_{station}"],
                               model.getVarByName(f"charge_time[{station}]"), incoming, arrival_at_station)
        # Infer the actual departure used by the model from its next arrival
        # plus the energy of this specific outbound arc in the release.
        after_station = at_active_arc(battery_rows(station, successor),
                                      model.getVarByName(f"battery[{successor}]"), outgoing, arrival_at_station)
        next_terminal = mapped.solver_to_terminal[successor]
        meters = task.matrices["shortest_distance_m"][station_idx, next_terminal]
        return dict(energy_kwh=departure - model_arrival, charge_s=charge,
                    departure_kwh=after_station + reserve + float(_arc_energy(meters, v)),
                    outbound_terminal=next_terminal, evaluated_rows="active Gurobi battery and charging constraints")


def _ev_model(task, solver):
    v, reserve, power = _charging_resources(task)
    nodes = task.nodes
    n = task.info["customer_count"]
    dist = task.matrices["shortest_distance_m"]
    travel = task.matrices["shortest_distance_time_s"]
    energy = _arc_energy(dist, v)
    coordinates = np.column_stack([nodes[key].to_numpy() for key in ("lon", "lat")])
    tw = np.column_stack([nodes[key].to_numpy()[1:n + 1] for key in ("ready_s", "due_s")])
    # The upstream battery[.] is charge above reserve: capacity shifts down by
    # exactly the same reserve at every node, leaving the missing kWh unchanged.
    usable = v["battery_capacity_kwh"] - reserve
    instance = EVRPTWInstance(
        instance_id=task.info["instance_id"], region_id=task.info["city"],
        mother_board_id="", operating_day_id="", day_type=task.info.get("day_type", ""),
        working_start_s=v["operating_window_s"][0], working_end_s=v["operating_window_s"][1],
        depot=coordinates[0], customers=coordinates[1:n + 1], charging_stations=coordinates[n + 1:],
        distance_matrix_km=dist / 1000.0, demands_cm3=nodes["demand_cm3"].to_numpy()[1:n + 1],
        package_counts=np.empty(0), service_time_s=nodes["service_s"].to_numpy()[1:n + 1],
        tw_s=tw, cs_time_to_depot_s=travel[n + 1:, 0],
        vehicle={**v, "battery_capacity_kwh": usable}, raw_travel_time_matrix_s=travel,
        energy_matrix_kwh=energy, raw={"charging_power_kw": power,
                                      "charging_policy": {"charging_power_derating_factor": 1.0}},
    )
    model, mapped, x, *_ = solver._build_model(instance)
    if v["initial_soc"] != 1.0:
        # Upstream assumes depot and CS both depart full. Only depot
        # departure uses initial_soc; all station departure equations retain full charge.
        model.update()
        deficit = v["battery_capacity_kwh"] * (1.0 - v["initial_soc"])
        model.getConstrByName("start_battery").RHS = usable - deficit
        for i, j in x:
            if i == mapped.start_depot:
                for side in ("lower", "upper"):
                    constraint = model.getConstrByName(f"battery_{side}_{i}_{j}")
                    constraint.RHS -= deficit
    return model, mapped, x


def _classical_model(task, view):
    # Capacity and service-time projections of the upstream start/end-depot
    # arc-flow model. The formulation uses one binary per directed arc.
    n = task.info["customer_count"]
    end = n + 1
    customers = list(range(1, n + 1))
    terminal = [0, *customers, 0]
    mapped = NodeMap(terminal, customers, [], 0, end)
    model = gp.Model(f"{view}_{task.info['instance_id']}")
    v = json.loads(task.info["vehicle_json"])
    dist = task.matrices["shortest_distance_m"]
    demand = task.nodes["demand_cm3"].to_numpy()[terminal] / v["cargo_capacity_cm3"]
    with_time = view == "vrptw"
    if with_time:
        travel = task.matrices["shortest_distance_time_s"]
        service, ready, due = (task.nodes[k].to_numpy()[terminal].copy()
                               for k in ("service_s", "ready_s", "due_s"))
        start, finish = v["operating_window_s"]
        ready[0] = due[0] = ready[end] = start
        due[end] = finish
    arcs = []
    for i in range(end):
        for j in range(1, end + 1):
            if i == j or (i == 0 and j == end) or demand[i] + demand[j] > 1.0 + 1e-12:
                continue
            if with_time and ready[i] + service[i] + travel[terminal[i], terminal[j]] > due[j] + 1e-8:
                continue
            arcs.append((i, j))
    x = model.addVars(arcs, vtype=GRB.BINARY, name="x")
    model.setObjective(gp.quicksum(dist[terminal[i], terminal[j]] / 1000.0 * x[i, j]
                                   for i, j in arcs), GRB.MINIMIZE)
    model.addConstrs((x.sum("*", j) == 1 for j in customers), name="customer_in")
    model.addConstrs((x.sum(i, "*") == 1 for i in customers), name="customer_out")
    model.addConstr(x.sum(0, "*") == x.sum("*", end), name="depot_balance")
    load = model.addVars(range(end + 1), lb=demand.tolist(), ub=1.0, name="load")
    load[0].UB = 0.0
    for i, j in arcs:
        # load[j] >= demand[j] is already a bound, so M = 1 suffices.
        model.addConstr(load[j] >= load[i] + demand[j] - (1.0 - x[i, j]))
    if with_time:
        tau = model.addVars(range(end + 1), lb=ready.tolist(), ub=due.tolist(), name="tau")
        for i, j in arcs:
            duration = service[i] + travel[terminal[i], terminal[j]]
            big_m = max(0.0, due[i] + duration - ready[j])
            model.addConstr(tau[j] >= tau[i] + duration - big_m * (1.0 - x[i, j]))
    return model, mapped, x


def _warm_start(task, view, model, mapped, x, ctx):
    # Build a new nearest-feasible-customer start from this method's input.
    # Checking the possible return leg here permits ending any partial route.
    vehicle = json.loads(task.info["vehicle_json"])
    cap = vehicle["cargo_capacity_cm3"]
    dist = task.matrices["shortest_distance_m"]
    demand = task.nodes["demand_cm3"].to_numpy()
    timed = view != "cvrp"
    if timed:
        time_matrix = task.matrices["shortest_distance_time_s"]
        ready, due, service = (task.nodes[k].to_numpy() for k in ("ready_s", "due_s", "service_s"))
        start, finish = vehicle["operating_window_s"]
    if view == "evrptw":
        usable = vehicle["battery_capacity_kwh"] * (vehicle["initial_soc"] - vehicle["reserve_soc"])
    left = set(mapped.customer_nodes)
    arcs = set()
    while left:
        i = 0
        load = 0.0
        distance = 0.0
        departure = start if timed else 0.0
        while left:
            if ctx.remaining() <= 1.0:
                return False
            best = None
            for j in left:
                if demand[j] + load > cap:
                    continue
                dij = dist[i, j]
                if timed:
                    t = max(ready[j], departure + time_matrix[i, j])
                    if t > due[j] or t + service[j] + time_matrix[j, 0] > finish:
                        continue
                if view == "evrptw" and _arc_energy(distance + dij + dist[j, 0], vehicle) > usable:
                    continue
                key = (dij, j)
                if best is None or key < best:
                    best = key
            if best is None:
                break
            _, j = best
            if timed:
                departure = max(ready[j], departure + time_matrix[i, j]) + service[j]
            load += demand[j]
            distance += dist[i, j]
            arcs.add((i, j))
            left.remove(j)
            i = j
        if i == 0:
            return False
        arcs.add((i, mapped.end_depot))
    if not arcs.issubset(x):
        return False
    model.setAttr("Start", list(x.values()), [float(arc in arcs) for arc in x])
    return True


def _separate(model, mapped, x, values):
    # Cut disconnected cycles (including zero-demand/zero-time nodes) and
    # paths that would allocate a vehicle to CS visits alone.
    selected = {arc for arc, value in values.items() if value > 0.5}
    successor = {i: j for i, j in selected if i != 0}
    remaining = set(selected)
    invalid = False
    for first in [j for i, j in selected if i == 0]:
        path = [(0, first)]
        node = first
        while node != mapped.end_depot:
            nxt = successor[node]
            path.append((node, nxt))
            node = nxt
        remaining.difference_update(path)
        if not any(j in mapped.customer_nodes for _, j in path):
            model.cbLazy(gp.quicksum(x[e] for e in path) <= len(path) - 1)
            invalid = True
    while remaining:
        begin, _ = next(iter(remaining))
        i = begin
        cycle = []
        while True:
            j = successor[i]
            remaining.remove((i, j))
            cycle.append((i, j))
            i = j
            if i == begin:
                break
        model.cbLazy(gp.quicksum(x[e] for e in cycle) <= len(cycle) - 1)
        invalid = True
    return invalid


def _bound_m(value):
    return float(value * 1000.0) if math.isfinite(value) and abs(value) < GRB.INFINITY else None


def solve(method, task, ctx):
    view = ctx.view
    scope = "restricted_to_2_visits_per_physical_station_across_fleet" if view == "evrptw" else "full_model"
    solver = GurobiEVRPTWSolver(GurobiSolverConfig(time_limit_s=None, cs_copies=2, threads=1, output_flag=0))
    ctx.info(solver_version=".".join(map(str, gp.gurobi.version())), source_commit=GROUP_COMMIT,
             station_copies_per_physical_station=2 if view == "evrptw" else 0, bound_scope=scope)
    model, mapped, x = _ev_model(task, solver) if view == "evrptw" else _classical_model(task, view)
    with model:
        model.Params.OutputFlag = 0
        model.Params.Threads = 1
        model.Params.Seed = ctx.seed
        model.Params.LazyConstraints = 1
        model.Params.MIPGap = 0
        model.Params.IntFeasTol = 1e-9
        model.Params.FeasibilityTol = 1e-8
        warm_start = _warm_start(task, view, model, mapped, x, ctx)
        ctx.info(mip_start_supplied=warm_start, model_status="optimizing")
        last_bound_at = -math.inf
        last_upper = math.inf
        callback_errors = []

        def callback(m, where):
            nonlocal last_bound_at, last_upper
            try:
                if where == GRB.Callback.MIPSOL:
                    values = m.cbGetSolution(x)
                    if _separate(m, mapped, x, values):
                        return
                    upper = _bound_m(m.cbGet(GRB.Callback.MIPSOL_OBJ))
                    if upper is not None and upper < last_upper:
                        routes = solver._extract_routes_from_arc_values(mapped, values)
                        lower = _bound_m(m.cbGet(GRB.Callback.MIPSOL_OBJBND))
                        ctx.emit(routes, upper_bound_m=upper, lower_bound_m=lower, bound_scope=scope)
                        last_upper = upper
                elif where == GRB.Callback.MIP and ctx.elapsed() - last_bound_at >= 1.0:
                    lower = _bound_m(m.cbGet(GRB.Callback.MIP_OBJBND))
                    if lower is not None:
                        ctx.info(lower_bound_m=lower, model_status="optimizing")
                    last_bound_at = ctx.elapsed()
                if ctx.remaining() <= 0.15:
                    m.terminate()
            except Exception as exc:
                callback_errors.append(exc)
                m.terminate()

        # Leave a small margin for Gurobi's soft timeout and final metadata.
        model.Params.TimeLimit = max(0.0, ctx.remaining() - 1.0)
        model.optimize(callback)
        if callback_errors:
            raise RuntimeError("Gurobi incumbent callback failed") from callback_errors[0]
        solved = model.SolCount > 0
        upper = _bound_m(model.ObjVal) if solved else None
        lower = _bound_m(model.ObjBound) if model.IsMIP and model.Status != GRB.LOADED else None
        status = solver._status_name(model.Status)
        if solved and upper < last_upper:
            ctx.emit(solver._extract_routes(mapped, x), upper_bound_m=upper, lower_bound_m=lower,
                     bound_scope=scope)
        ctx.info(model_status=status, solver_status=status, upper_bound_m=upper, lower_bound_m=lower,
                 mip_gap=float(model.MIPGap) if solved and math.isfinite(model.MIPGap) else None,
                 bound_scope=scope, solution_count=int(model.SolCount))
