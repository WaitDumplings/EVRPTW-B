"""CPU adapters for the directed CVRP and VRPTW Parquet views."""

from importlib.metadata import version
import json
import math

import numpy as np


def _integers(task):
    # Objective in millimetres. For resource constraints use inward rounding.
    # Keep sub-cm³ demand, millisecond time, and inclusive window endpoints.
    cols = task.nodes.to_pydict()
    data = {
        "distance": np.rint(task.matrices["shortest_distance_m"] * 1000).astype(np.int64),
        "demand": np.ceil(np.array(cols["demand_cm3"]) * 1000).astype(np.int64),
        "capacity": math.floor(task.info["capacity_cm3"] * 1000),
    }
    if task.info["problem_type"] == "vrptw":
        start, end = json.loads(task.info["vehicle_json"])["operating_window_s"]
        data.update(
            duration=np.ceil(task.matrices["shortest_distance_time_s"] * 1000).astype(np.int64),
            service=np.ceil(np.array(cols["service_s"]) * 1000).astype(np.int64),
            early=np.ceil(np.array(cols["ready_s"]) * 1000).astype(np.int64),
            late=np.floor(np.array(cols["due_s"]) * 1000).astype(np.int64),
            shift=(math.ceil(start * 1000), math.floor(end * 1000)),
        )
    return data


def _ortools_model(task, seed):
    from ortools.constraint_solver import pywrapcp

    a = _integers(task)
    n = task.nodes.num_rows
    vehicles = n - 1
    manager = pywrapcp.RoutingIndexManager(n, vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)
    routing.solver().ReSeed(seed)
    distance = routing.RegisterTransitMatrix(a["distance"].tolist())
    routing.SetArcCostEvaluatorOfAllVehicles(distance)
    loads = routing.RegisterUnaryTransitVector(a["demand"].tolist())
    routing.AddDimensionWithVehicleCapacity(loads, 0, [a["capacity"]] * vehicles, True, "Cargo")
    if "duration" in a:
        travel = routing.RegisterTransitMatrix((a["duration"] + a["service"][:, None]).tolist())
        start, end = a["shift"]
        routing.AddDimension(travel, end - start, end, False, "Time")
        times = routing.GetDimensionOrDie("Time")
        for node in range(1, n):
            times.CumulVar(manager.NodeToIndex(node)).SetRange(int(a["early"][node]), int(a["late"][node]))
        for k in range(vehicles):
            times.CumulVar(routing.Start(k)).SetRange(start, end)
            times.CumulVar(routing.End(k)).SetRange(start, end)

    def routes_at(value):
        routes = []
        for k in range(vehicles):
            route = [0]
            idx = value(routing.NextVar(routing.Start(k)))
            while not routing.IsEnd(idx):
                route.append(manager.IndexToNode(idx))
                idx = value(routing.NextVar(idx))
            if len(route) > 1:
                routes.append([*route, 0])
        return routes

    return routing, routes_at


def _ortools_parameters(seed):
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.sat_parameters.num_workers = 1
    params.sat_parameters.random_seed = seed
    return params


def _ortools(task, ctx):
    from ortools.constraint_solver import routing_enums_pb2

    routing, routes_at = _ortools_model(task, ctx.seed)
    lowest = math.inf

    def at_solution():
        nonlocal lowest
        cost = routing.CostVar().Value()
        if cost < lowest:
            ctx.emit(routes_at(lambda var: var.Value()), objective_mm=cost)
            lowest = cost

    routing.AddAtSolutionCallback(at_solution)
    params = _ortools_parameters(ctx.seed)
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    ctx.info(version=version("ortools"), algorithm="Routing Solver GLS", **_scales(task))
    ms = int((ctx.remaining() - 0.08) * 1000)
    if ms <= 0:
        return
    params.time_limit.FromMilliseconds(ms)
    answer = routing.SolveWithParameters(params)
    if answer is not None and answer.ObjectiveValue() < lowest:
        ctx.emit(routes_at(answer.Value), objective_mm=answer.ObjectiveValue())


def _scales(task):
    return dict(distance_units="round(m * 1000)", demand_units="ceil(cm3 * 1000), floor capacity",
                time_units="ceil travel/service/ready, floor due, milliseconds"
                if task.info["problem_type"] == "vrptw" else None,
                vehicle_upper_bound=task.nodes.num_rows - 1, fixed_vehicle_cost=0)


def solve(method, task, ctx):
    if method == "ortools" and task.info["problem_type"] in ("cvrp", "vrptw"):
        return _ortools(task, ctx)
    if method == "hgs" and task.info["problem_type"] == "cvrp":
        from .hgs_matrix import solve_hgs
        return solve_hgs(task, ctx)
    if method in ("pyvrp-ils", "pyvrp-hgs") and task.info["problem_type"] in ("cvrp", "vrptw"):
        if method == "pyvrp-hgs" and task.info["problem_type"] != "vrptw":
            raise ValueError("PyVRP-HGS acceptance is defined only for VRPTW")
        from .pyvrp_adapters import solve_pyvrp
        return solve_pyvrp(method, task, ctx)
    raise ValueError(f"Unsupported method/view: {method}/{task.info['problem_type']}")
