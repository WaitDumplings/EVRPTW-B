"""Synthetic instances in the adapter's native grammar, and an independent route checker."""
import json, math, random, sys
import numpy as np

def generate(path, n, m, seed, range_frac, tw_frac, cap_routes):
    rng = random.Random(seed); size = n + m + 1
    pts = [(rng.uniform(0, 30), rng.uniform(0, 30)) for _ in range(size)]
    d = np.zeros((size, size))
    for i in range(size):
        for j in range(size):
            if i != j:
                d[i, j] = math.dist(pts[i], pts[j]) * rng.uniform(1.0, 1.6)   # directed detours
    for k in range(size):                                                       # shortest-path closure
        d = np.minimum(d, d[:, [k]] + d[[k], :])
    speed = 30 / 3600.0                                                        # km per second
    t = d / speed * np.array([[rng.uniform(0.9, 1.2) for _ in range(size)] for _ in range(size)])
    np.fill_diagonal(t, 0)
    rate = 0.39; e = d * rate
    dem = [0.0] + [rng.uniform(5e4, 3e5) for _ in range(n)]
    cap = sum(dem) / cap_routes * 1.15
    start, end = 8 * 3600.0, 24 * 3600.0
    tour = float(np.sort(d, axis=1)[:, 1].sum())                                # rough tour length
    q = max(range_frac * tour * rate, 2.2 * float(e.max()))
    with open(path, "w") as f:
        f.write(f"NAME : synth-{n}-{seed}\nTYPE : EVRPTW-ICLR-REFERENCE\nDIMENSION : {size}\n")
        for k, v in dict(VEHICLES=n + 1, DISPATCHINGCOST=0, UNITCOST=1, CAPACITY=cap, ELECTRIC_POWER=q,
                         CONSUMPTION_RATE=1, RECHARGING_RATE=1).items():
            f.write(f"{k} : {v:.17g}\n")
        f.write("EDGE_WEIGHT_TYPE : EXPLICIT\nNODE_SECTION\nID,type,x,y,delivery,pickup,ready_time,due_date,service_time\n")
        for i, (x, y) in enumerate(pts):
            kind = "d" if i == 0 else "c" if i <= n else "f"
            if kind == "c":
                if rng.random() < tw_frac:
                    a = rng.uniform(start, end - 4 * 3600); ready, due = a, a + rng.uniform(1800, 7200)
                else:
                    ready, due = start, end
                vals = [x, y, dem[i], 0.0, ready, due, rng.uniform(60, 300)]
            else:
                vals = [x, y, 0.0, 0.0, start, end, 0.0]
            f.write(f"{i},{kind}," + ",".join(format(v, ".17g") for v in vals) + "\n")
        f.write("DISTANCETIME_SECTION\nID,from_node,to_node,distance,spend_tm,energy_kwh\n")
        for i in range(size):
            for j in range(size):
                f.write(f"{i*size+j},{i},{j},{d[i,j]:.17g},{t[i,j]:.17g},{e[i,j]:.17g}\n")
        f.write("STATION_CHARGING_SECONDS_PER_KWH\n")
        for j in range(n + 1, size):
            f.write(f"{j},{3600.0/rng.choice([50, 100, 150]):.17g}\n")
        f.write("DEPOT_SECTION\n0\n")

def load(path):
    head, nodes, arcs, rates, sec = {}, [], [], {}, None
    for line in open(path):
        line = line.strip()
        if " : " in line and sec is None:
            k, v = line.split(" : "); head[k] = v; continue
        if line in ("NODE_SECTION", "DISTANCETIME_SECTION", "STATION_CHARGING_SECONDS_PER_KWH", "DEPOT_SECTION"):
            sec = line; continue
        if line.startswith("ID,") or not line: continue
        p = line.split(",")
        if sec == "NODE_SECTION": nodes.append((p[1], *map(float, p[2:])))
        elif sec == "DISTANCETIME_SECTION": arcs.append((int(p[1]), int(p[2]), float(p[3]), float(p[4]), float(p[5])))
        elif sec == "STATION_CHARGING_SECONDS_PER_KWH": rates[int(p[0])] = float(p[1])
    size = int(head["DIMENSION"]); D = np.zeros((size, size)); T = D.copy(); E = D.copy()
    for a, b, d, t, e in arcs: D[a, b], T[a, b], E[a, b] = d, t, e
    return dict(C=float(head["CAPACITY"]), Q=float(head["ELECTRIC_POWER"]), nodes=nodes, D=D, T=T, E=E, rates=rates)

def check(inst, routes):
    """Hard-constraint replay written independently of the solver. Returns (distance, charges)."""
    nodes, D, T, E = inst["nodes"], inst["D"], inst["T"], inst["E"]
    customers = [i for i, x in enumerate(nodes) if x[0] == "c"]
    seen, total, charges = [], 0.0, 0
    for r in routes:
        assert r[0] == 0 and r[-1] == 0 and len(r) > 2, "route must start and end at the depot"
        assert 0 not in r[1:-1], "depot inside a route"
        clock, battery, load = nodes[0][5], inst["Q"], 0.0
        for a, b in zip(r, r[1:]):
            total += D[a, b]; clock += T[a, b]; battery -= E[a, b]
            assert battery >= -1e-8, f"battery below reserve on arc {a}->{b}: {battery}"
            kind, _, _, demand, _, ready, due, service = nodes[b]
            clock = max(clock, ready)
            assert clock <= due + 1e-6, f"late at {b}: {clock} > {due}"
            if kind == "c":
                seen.append(b); load += demand; clock += service
            elif kind == "f":
                clock += inst["rates"][b] * (inst["Q"] - battery); battery = inst["Q"]; charges += 1
        assert load <= inst["C"] + 1e-6, "capacity exceeded"
    assert sorted(seen) == customers, "every customer exactly once"
    return total, charges

if __name__ == "__main__":
    if sys.argv[1] == "gen":
        generate(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]), float(sys.argv[6]), float(sys.argv[7]), float(sys.argv[8]))
    else:
        inst = load(sys.argv[2]); events = [json.loads(x) for x in open(sys.argv[3])]
        prev = float("inf")
        for ev in events:
            dist, charges = check(inst, ev["routes"])
            assert abs(dist - ev["internal_objective"]) < 1e-6 * max(1, dist), (dist, ev["internal_objective"])
            assert dist < prev; prev = dist
        last = events[-1]
        print(f"events={len(events)} all verified; first={events[0]['internal_objective']:.3f} km at {events[0]['elapsed_s']:.2f}s; "
              f"best={last['internal_objective']:.3f} km at {last['elapsed_s']:.2f}s; routes={len(last['routes'])}; charges={check(inst, last['routes'])[1]}")
