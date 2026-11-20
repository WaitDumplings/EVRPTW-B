"""Evaluate source node indices against the original, directed float64 matrices."""
import json
import math
from numbers import Integral


class InvalidRoute(ValueError):
    pass


def evaluate(task, routes, *, trace=False):
    view = task.info["problem_type"]
    vehicle = json.loads(task.info["vehicle_json"])
    rows = task.nodes.to_pylist()
    if not rows or rows[0]["type"] != "depot" or any(r["node_idx"] != i for i, r in enumerate(rows)):
        raise ValueError("source node indices or depot do not match the matrix")
    customers = {i for i, r in enumerate(rows) if r["type"] == "customer"}
    served = set()
    distance = task.matrices["shortest_distance_m"]
    times = task.matrices.get("shortest_distance_time_s")
    electric = view == "evrptw"
    timed = view != "cvrp"
    capacity = vehicle["cargo_capacity_cm3"]
    if timed:
        start, end = vehicle["operating_window_s"]
    if electric:
        battery = vehicle["battery_capacity_kwh"]
        reserve = vehicle["reserve_soc"] * battery
    if not isinstance(routes, (tuple, list)) or not routes:
        raise InvalidRoute("empty solution")
    distances, route_details = [], []
    station_count = 0
    for k, route in enumerate(routes, 1):
        if not isinstance(route, (tuple, list)) or len(route) < 3 or route[0] != 0 or route[-1] != 0:
            raise InvalidRoute(f"route {k} needs a depot at each end and at least one stop")
        if any(isinstance(i, bool) or not isinstance(i, Integral) or not 0 <= i < len(rows) for i in route):
            raise InvalidRoute(f"route {k} contains an invalid source index")
        if not any(i in customers for i in route[1:-1]):
            raise InvalidRoute(f"route {k} serves no customer")
        if 0 in route[1:-1]:
            raise InvalidRoute(f"route {k} has an interior depot")
        legs, loads, stops = [], [], []
        clock = start if timed else None
        energy = battery * vehicle["initial_soc"] if electric else None
        for a, b in zip(route, route[1:]):
            meters = float(distance[a, b])
            if meters < 0 or not math.isfinite(meters):
                raise InvalidRoute(f"invalid directed arc {a}->{b}")
            legs.append(meters)
            row = rows[b]
            service_start = arrival = arrival_energy = None
            if timed:
                seconds = float(times[a, b])
                if seconds < 0 or not math.isfinite(seconds):
                    raise InvalidRoute(f"invalid travel time {a}->{b}")
                arrival = clock + seconds
                clock = arrival
            if electric:
                energy -= meters / 1000 * vehicle["specific_energy_kwh_per_km"]
                arrival_energy = energy
                if energy < reserve - 1e-8:
                    raise InvalidRoute(f"route {k} arrives at {b} below reserve ({energy:.9f} kWh)")
            if row["type"] == "customer":
                if b in served:
                    raise InvalidRoute(f"customer {b} visited more than once")
                served.add(b)
                loads.append(float(row["demand_cm3"]))
                if timed:
                    service_start = max(clock, row["ready_s"])
                    if service_start > row["due_s"] + 1e-8:
                        raise InvalidRoute(f"late service start at customer {b}")
                    clock = service_start + row["service_s"]
            elif row["type"] == "charging" and electric:
                power = row["charge_kw"]
                if not math.isfinite(power) or power <= 0:
                    raise InvalidRoute(f"station {b} has invalid effective power")
                clock += 3600 * (battery - energy) / power
                energy = battery
                station_count += 1
            elif b != 0:
                raise InvalidRoute(f"route {k} has an unsupported stop {b}")
            if trace:
                stops.append(dict(origin=a, destination=b, meters=meters, arrival_s=arrival,
                                  service_start_s=service_start, departure_s=clock,
                                  arrival_kwh=arrival_energy, departure_kwh=energy))
        total_load = math.fsum(loads)
        if total_load > capacity + 1e-6:
            raise InvalidRoute(f"route {k} exceeds cargo capacity")
        if timed and clock > end + 1e-8:
            raise InvalidRoute(f"route {k} returns after shift end")
        distances.append(math.fsum(legs))
        if trace:
            route_details.append(dict(route=k, load_cm3=total_load, meters=distances[-1],
                                      return_s=clock, legs=stops))
    if served != customers:
        raise InvalidRoute(f"missing {len(customers-served)} customers: {sorted(customers-served)[:10]}")
    result = dict(valid=True, distance_m=math.fsum(distances), vehicles=len(routes),
                  charging_visits=station_count if electric else None, customers=len(served))
    if trace:
        result["routes"] = route_details
    return result


def station_probe(task):
    rows = task.nodes.to_pylist()
    v = json.loads(task.info["vehicle_json"])
    battery = v["battery_capacity_kwh"]
    # Prefer an actual depot-to-station approach that reaches the station above reserve.
    for a, row in enumerate(rows):
        if row["type"] == "charging":
            continue
        for b, station in enumerate(rows):
            if station["type"] != "charging" or station["charge_kw"] <= 0:
                continue
            meters = float(task.matrices["shortest_distance_m"][a, b])
            used = meters * v["specific_energy_kwh_per_km"] / 1000
            arrival = battery * v["initial_soc"] - used
            if used > 0 and arrival >= battery * v["reserve_soc"] and arrival < battery:
                return dict(origin=a, station=b, distance_m=meters, effective_kw=station["charge_kw"],
                            arrival_kwh=arrival, energy_kwh=used, departure_kwh=battery,
                            charge_s=(battery-arrival) * 3600 / station["charge_kw"])
    raise ValueError("no reachable nonzero arc to a charging station for probe")


def main():
    import argparse
    from pathlib import Path
    from runtime import file_hash, load_view

    parser = argparse.ArgumentParser(description="Replay an exported solution on its original input.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--routes", type=Path, required=True)
    parser.add_argument("--trace", action="store_true", help="include every directed leg and resource state")
    args = parser.parse_args()
    solution = json.loads(args.routes.read_text())
    if file_hash(args.input) != solution["sha256"]:
        parser.error("route and input SHA-256 differ")
    task = load_view(args.input, solution["view"])
    if task.info["instance_id"] != solution["instance_id"]:
        parser.error("route and input instance IDs differ")
    print(json.dumps(evaluate(task, solution["routes"], trace=args.trace), allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
