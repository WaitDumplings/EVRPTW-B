"""Post-run evaluation of the unmodified routes produced by the three constructors."""
from collections import Counter
import json
import math
from numbers import Integral

from replay import InvalidRoute, evaluate

INITIAL_PAIRS = frozenset({
    ("ortools", "cvrp"), ("ortools", "vrptw"),
    ("vns-ts", "evrptw"), ("routingblocks", "evrptw"),
})
SCHEMA = "evrptwb_initial_solution_v1"
IDENTITY = ("instance_id", "sha256", "method", "view", "seed", "seconds")


def matches_initial(record, result):
    return (record.get("schema") == SCHEMA
            and record.get("status") in {"recorded", "no_initial_solution", "unknown"}
            and all(record.get(k) == result[k] for k in IDENTITY))


def measure_routes(task, routes):
    """Measure the supplied walk even when the public replay rejects a constraint."""
    rows = task.nodes.to_pylist()
    view = task.info["problem_type"]
    timed, electric = view != "cvrp", view == "evrptw"
    vehicle = json.loads(task.info["vehicle_json"])
    distance = task.matrices["shortest_distance_m"]
    durations = task.matrices.get("shortest_distance_time_s")
    coverage = Counter()
    loads, late_visits, late_returns, shortfalls, lengths = [], [], [], [], []
    station_visits = 0
    shape = Counter()
    for route in routes:
        if any(isinstance(i, bool) or not isinstance(i, Integral) or not 0 <= i < len(rows)
               for i in route):
            raise ValueError("Constructor returned an unknown node; directed distance is undefined")
        if len(route) < 3 or route[0] != 0 or route[-1] != 0:
            shape["invalid_endpoints_or_length"] += 1
        if 0 in route[1:-1]:
            shape["interior_depots"] += route[1:-1].count(0)
        if not any(rows[i]["type"] == "customer" for i in route[1:-1]):
            shape["routes_without_customers"] += 1
        demand = []
        if timed:
            clock, shift_end = vehicle["operating_window_s"]
        if electric:
            battery = vehicle["battery_capacity_kwh"]
            energy = vehicle["initial_soc"] * battery
            reserve = vehicle["reserve_soc"] * battery
        legs = []
        for a, b in zip(route, route[1:]):
            meters = float(distance[a, b])
            legs.append(meters)
            row = rows[b]
            if timed:
                clock += float(durations[a, b])
            if electric:
                energy -= meters / 1000 * vehicle["specific_energy_kwh_per_km"]
                if energy < reserve - 1e-8:
                    shortfalls.append(reserve - energy)
            if row["type"] == "customer":
                coverage[b] += 1
                demand.append(row["demand_cm3"])
                if timed:
                    clock = max(clock, row["ready_s"])
                    if clock > row["due_s"] + 1e-8:
                        late_visits.append(clock - row["due_s"])
                    clock += row["service_s"]
            elif row["type"] == "charging" and electric:
                station_visits += 1
                clock += 3600 * (battery - energy) / row["charge_kw"]
                energy = battery
        lengths.append(math.fsum(legs))
        load = math.fsum(demand)
        if load > vehicle["cargo_capacity_cm3"] + 1e-6:
            loads.append(load - vehicle["cargo_capacity_cm3"])
        if timed and clock > shift_end + 1e-8:
            late_returns.append(clock - shift_end)

    missing = sum(i not in coverage for i, row in enumerate(rows) if row["type"] == "customer")
    repeated = sum(n - 1 for n in coverage.values())
    violations = {}
    if shape:
        violations["route_structure"] = dict(shape)
    if missing or repeated:
        violations["customer_coverage"] = dict(missing_customers=missing, duplicate_visits=repeated)
    if loads:
        violations["capacity"] = dict(routes=len(loads), total_excess_cm3=math.fsum(loads))
    if late_visits or late_returns:
        violations["time_window"] = dict(late_customer_visits=len(late_visits),
                                         customer_lateness_s=math.fsum(late_visits),
                                         late_returns=len(late_returns),
                                         shift_overtime_s=math.fsum(late_returns))
    if shortfalls:
        violations["energy"] = dict(arrivals_below_reserve=len(shortfalls),
                                    sum_arrival_deficits_kwh=math.fsum(shortfalls),
                                    max_arrival_deficit_kwh=max(shortfalls))
    distance_m = math.fsum(lengths)
    try:
        checked = evaluate(task, routes)
    except InvalidRoute as error:
        feasible, reason = False, str(error)
    else:
        if violations or checked["distance_m"] != distance_m:
            raise ValueError("Initial-route diagnostics disagree with independent replay")
        feasible, reason = True, None
    return dict(distance_m=distance_m, vehicles=len(routes), charging_visits=station_visits,
                feasible=feasible, violations=violations, replay_error=reason)
