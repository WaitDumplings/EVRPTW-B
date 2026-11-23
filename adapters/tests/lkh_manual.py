"""Check independent VRPTW times against the two possible directed tours."""
import argparse
import hashlib
from itertools import permutations
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
DEPS = Path(os.environ.get("EVB_DEPS", ROOT / ".deps"))
DISTANCE = ((0, 1, 5), (5, 0, 1), (1, 5, 0))
TRAVEL_TIME = ((0, 12, 2), (2, 0, 12), (12, 2, 0))
WINDOWS = ((0, 40), (0, 10), (0, 40))
SEED = 20261103


def evaluate(order):
    route = [0, *order, 0]
    arrival, cost, clock, late = [], 0, 0, 0
    for origin, dest in zip(route, route[1:]):
        cost += DISTANCE[origin][dest]
        clock += TRAVEL_TIME[origin][dest]
        arrival.append(clock)
        ready, due = WINDOWS[dest]
        clock = max(clock, ready)
        late += max(0, clock - due)
    return dict(route=route, arrivals=arrival, cost=cost, lateness=late)


def matrix_rows(matrix):
    return "\n".join(" ".join(map(str, row)) for row in matrix)


def write_inputs(out):
    problem = f"""NAME: manual-independent-time
TYPE: CVRPTW
DIMENSION: 3
CAPACITY: 2
VEHICLES: 1
EDGE_WEIGHT_TYPE: EXPLICIT
EDGE_WEIGHT_FORMAT: FULL_MATRIX
EDGE_WEIGHT_SECTION
{matrix_rows(DISTANCE)}
TRAVEL_TIME_SECTION
{matrix_rows(TRAVEL_TIME)}
DEMAND_SECTION
1 0
2 1
3 1
DEPOT_SECTION
1
-1
SERVICE_TIME: 0
TIME_WINDOW_SECTION
{matrix_rows([(i, *window) for i, window in enumerate(WINDOWS, 1)])}
EOF
"""
    (out / "manual.vrp").write_text(problem, encoding="ascii")
    (out / "solver.par").write_text(f"""PROBLEM_FILE = manual.vrp
MTSP_MAX_SIZE = 2
RUNS = 2
MAX_TRIALS = 12
SEED = {SEED}
STOP_AT_OPTIMUM = NO
TRACE_LEVEL = 2
TOUR_FILE = best.tour
""", encoding="ascii")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path, help="fresh directory for the input, log, tour and evidence")
    args = p.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    write_inputs(out)
    tours = [evaluate(order) for order in permutations((1, 2))]
    feasible = [tour for tour in tours if tour["lateness"] == 0]
    if feasible != [dict(route=[0, 2, 1, 0], arrivals=[2, 4, 6], cost=15, lateness=0)]:
        raise AssertionError(f"Unexpected enumeration: {tours}")
    (out / "enumeration.json").write_text(json.dumps(tours, indent=2) + "\n")

    generation = (DEPS / "lkh-3.0.10/bin").resolve(strict=True)
    binary = generation / "LKH-vrptw-time"
    receipt = json.loads((generation / "preparation.json").read_text())
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if digest != receipt["binaries_sha256"][binary.name]:
        raise ValueError("Prepared VRPTW binary SHA-256 mismatch")
    cpu = min(os.sched_getaffinity(0))
    os.sched_setaffinity(0, {cpu})
    log = out / "stdout.log"
    with log.open("wb") as f:
        subprocess.run([str(binary), "solver.par"], cwd=out, stdin=subprocess.DEVNULL,
                       stdout=f, stderr=subprocess.STDOUT, check=True)
    native = (out / "best.tour").read_text().splitlines()
    order = [int(x) - 1 for x in native[native.index("TOUR_SECTION") + 1:native.index("-1")]]
    runs = [tuple(map(int, x)) for x in re.findall(
        r"^Run (\d+):\s+Cost = (\d+)_(\d+)", log.read_text(), re.MULTILINE)]
    if order + [0] != feasible[0]["route"] or runs != [(1, 0, 15), (2, 0, 15)]:
        raise AssertionError(f"Unexpected solver route {order} or run statistics {runs}")
    if "COMMENT : Length = 15" not in native:
        raise AssertionError("Expected the native tour to report a distance of 15")
    result = dict(cpu=cpu, binary_sha256=digest, patch_sha256=receipt["inputs"]["patch_sha256"],
                  params="solver.par", enumeration=tours, run_penalty_cost=runs,
                  final_route=order + [0], arrivals=feasible[0]["arrivals"], objective=15)
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
