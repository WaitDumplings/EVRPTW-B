"""Compare every reported CVRPTW trial and the final directed tour of three LKH builds/inputs."""

import argparse
import csv
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import zipfile
from pathlib import Path

SOURCE_PAGE = "https://www.sintef.no/projectweb/top/vrptw/100-customers/"
SOURCE_ZIP = (
    "https://www.sintef.no/globalassets/project/top/vrptw/solomon/solomon-100.zip"
)
ARCHIVE_HASH = "8a0a72cbe6b7f8f9988ace4ebde0378ec34943acaaac47f2c408915e41887747"
CASES = ("C101", "R101", "RC101", "directed-empty")
MODES = ("original", "no_time", "equal")
RUNS, TRIALS, SEED = 2, 8, 20261103
PARAMETERS = f"""PROBLEM_FILE = problem.vrp
RUNS = {RUNS}
MAX_TRIALS = {TRIALS}
SEED = {SEED}
STOP_AT_OPTIMUM = NO
TRACE_LEVEL = 2
MTSP_MIN_SIZE = 0
TOUR_FILE = final.tour
"""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def matrix_text(matrix):
    return "\n".join(" ".join(map(str, row)) for row in matrix)


def inputs(case, archive):
    if case == "directed-empty":
        # Nine slots for eight customers force an empty route even if all are singletons.
        k, capacity, count = 9, 4, 9
        costs = [
            [0 if i == j else 3 + (5 * i + 7 * j + i * j) % 12 for j in range(count)]
            for i in range(count)
        ]
        loads = [0, 1, 1, 1, 1, 1, 1, 1, 1]
        starts = [0, 0, 60, 0, 40, 0, 60, 0, 30]
        ends = [300, 14, 135, 115, 120, 125, 140, 140, 165]
        services = [0] + [3] * 8
        weights = (
            "EDGE_WEIGHT_TYPE: EXPLICIT\nEDGE_WEIGHT_FORMAT: FULL_MATRIX\nEDGE_WEIGHT_SECTION\n"
            + matrix_text(costs)
        )
        source = {
            "kind": "synthetic",
            "description": "explicit directed arcs, 8 customers and 9 vehicle slots",
        }
    else:
        member = f"In/{case.lower()}.txt"
        data = archive.read(member)
        numeric = [
            [int(t) for t in line.split()]
            for line in data.decode("ascii").splitlines()
            if line.strip() and line.split()[0].isdigit()
        ]
        k, capacity = numeric[0]
        ids, x, y, loads, starts, ends, services = zip(*numeric[1:])
        if list(ids) != list(range(101)) or (k, capacity) != (25, 200):
            raise ValueError(f"Unexpected Solomon input in {member}")
        coords = list(zip(x, y))
        count = len(coords)
        # TSPLIB EUC_2D as computed by upstream Distance_EUC_2D at SCALE=1.
        costs = [
            [math.floor(math.hypot(ax - bx, ay - by) + 0.5) for bx, by in coords]
            for ax, ay in coords
        ]
        weights = "EDGE_WEIGHT_TYPE: EUC_2D\nNODE_COORD_SECTION\n" + "\n".join(
            f"{i} {cx} {cy}" for i, (cx, cy) in enumerate(coords, 1)
        )
        source = {
            "kind": "Solomon 100, native EUC_2D",
            "member": member,
            "member_sha256": sha(data),
        }
    head = (
        f"NAME: {case}\nTYPE: CVRPTW\nDIMENSION: {count}\nCAPACITY: {capacity}\n"
        f"VEHICLES: {k}\n{weights}\nDEMAND_SECTION\n"
    )
    head += "".join(f"{idx} {load}\n" for idx, load in enumerate(loads, 1))
    head += "DEPOT_SECTION\n1\n-1\nSERVICE_TIME_SECTION\n"
    head += "".join(f"{idx} {service}\n" for idx, service in enumerate(services, 1))
    head += "TIME_WINDOW_SECTION\n" + "".join(
        f"{idx} {a} {b}\n" for idx, (a, b) in enumerate(zip(starts, ends), 1)
    )
    stats = {
        "dimension": count,
        "vehicles": k,
        "capacity": capacity,
        "must_late_arcs": sum(
            i != j and starts[i] + services[i] + costs[i][j] > ends[j]
            for i in range(count)
            for j in range(count)
        ),
        "asymmetric_pairs": sum(
            costs[i][j] != costs[j][i] for i in range(count) for j in range(i)
        ),
        **source,
    }
    return (
        head + "EOF\n",
        head + "TRAVEL_TIME_SECTION\n" + matrix_text(costs) + "\nEOF\n",
        stats,
    )


def prepare(root, archive_path):
    if sha(archive_path.read_bytes()) != ARCHIVE_HASH:
        raise ValueError("Solomon archive SHA-256 mismatch")
    with zipfile.ZipFile(archive_path) as archive:
        for case in CASES:
            base, equal, stats = inputs(case, archive)
            for mode in MODES:
                dest = root / case / mode
                dest.mkdir(parents=True, exist_ok=False)
                (dest / "problem.vrp").write_text(equal if mode == "equal" else base)
                (dest / "test.par").write_text(PARAMETERS)
            save_json(root / case / "input.json", stats)
    save_json(
        root / "plan.json",
        {
            "cases": CASES,
            "modes": MODES,
            "parameters": PARAMETERS.splitlines(),
            "source_page": SOURCE_PAGE,
            "archive_url": SOURCE_ZIP,
            "archive_sha256": ARCHIVE_HASH,
        },
    )


def read_trace(log):
    records = []
    run = 1
    trial_line = re.compile(r"^\s*\*?\s*(\d+): Cost = (\d+)_(\d+)\b")
    run_line = re.compile(r"^Run (\d+): Cost = ")
    for line in log.splitlines():
        trial = trial_line.match(line)
        ended = run_line.match(line)
        if trial:
            records.append((run, *map(int, trial.groups())))
        elif ended:
            if int(ended[1]) != run:
                raise ValueError(f"Unexpected run ending {line}")
            run += 1
    keys = [item[:2] for item in records]
    expected = list(itertools.product(range(1, RUNS + 1), range(1, TRIALS + 1)))
    if keys != expected or run != RUNS + 1:
        raise ValueError(
            f"Expected {RUNS} complete runs of {TRIALS} trials; got {keys} and {run - 1} completed runs"
        )
    return records


def read_tour(path, dimension, vehicles):
    lines = path.read_text().splitlines()
    if lines[-2:] != ["-1", "EOF"]:
        raise ValueError(f"Incomplete tour in {path}")
    ids = list(map(int, lines[lines.index("TOUR_SECTION") + 1 : -2]))
    expected = dimension + vehicles - 1
    if (
        len(ids) != expected
        or sorted(ids) != list(range(1, expected + 1))
        or ids[0] != 1
    ):
        raise ValueError(f"Invalid directed tour in {path}")
    length = next(
        int(line.split("=")[1])
        for line in lines
        if line.startswith("COMMENT : Length =")
    )
    return ids, length


# Stop at the last statistics call after LKHmain has saved its global incumbent.
# Reading BestTour here also works for positive penalty, when WriteTour returns early.
# The observer leaves address randomization enabled and only reads solver data.
GDB_SCRIPT = """set pagination off
set confirm off
set disable-randomization off
break PrintStatistics
commands
silent
python
import gdb, json
value = gdb.parse_and_eval
n = int(value('DimensionSaved'))
tour = value('BestTour')
raw = [int(tour[i]) for i in range(1, n + 1)]
depot = int(value('MTSPDepot'))
if not int(value('Asymmetric')) or sorted(raw) != list(range(1, n + 1)):
    raise ValueError('Unexpected representation of the final CVRPTW tour')
offset = raw.index(depot)
# SRC/WriteTour.c prints forward starting at MTSPDepot for Asymmetric problems.
record = dict(raw=raw, ids=raw[offset:] + raw[:offset], depot=depot,
              penalty=int(value('BestPenalty')), cost=int(value('BestCost')))
with open('best-tour.json', 'x') as output:
    print(json.dumps(record), file=output)
end
continue
end
run
quit $_exitcode
"""


def run_one(directory, executable, stats, prior=None):
    log = directory / "solver.log"
    observer = directory / "capture.gdb"
    observer.write_text(GDB_SCRIPT)
    with log.open("x") as f:
        subprocess.run(
            [
                "gdb",
                "-quiet",
                "-nx",
                "-batch",
                "-x",
                str(observer.resolve()),
                "--args",
                str(executable),
                "test.par",
            ],
            cwd=directory,
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            check=True,
        )
    records = read_trace(log.read_text())
    with (directory / "trials.tsv").open("x", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(("run", "trial", "penalty", "cost"))
        writer.writerows(records)
    captured = json.loads((directory / "best-tour.json").read_text())
    ids = captured["ids"]
    penalty, length = captured["penalty"], captured["cost"]
    expected = stats["dimension"] + stats["vehicles"] - 1
    if sorted(ids) != list(range(1, expected + 1)) or ids[0] != 1:
        raise ValueError(f"{directory}: invalid observer tour")
    if min((p, c) for _, _, p, c in records) != (penalty, length):
        raise ValueError(
            f"{directory}: final penalty/cost differs from the trial records"
        )
    if penalty == 0 or (directory / "final.tour").exists():
        official = read_tour(
            directory / "final.tour", stats["dimension"], stats["vehicles"]
        )
        if official != (ids, length):
            raise ValueError(
                f"{directory}: debugger capture differs from native TOUR_SECTION"
            )
    if prior is not None:
        for name in ("problem.vrp", "test.par"):
            if (directory / name).read_bytes() != (prior / name).read_bytes():
                raise ValueError(f"{directory}: earlier input differs: {name}")
        if (prior / "solver.log").is_file() and read_trace(
            (prior / "solver.log").read_text()
        ) != records:
            raise ValueError(
                f"{directory}: trial results differ from the earlier debugger-free execution"
            )
        if (prior / "final.tour").is_file() and read_tour(
            prior / "final.tour", stats["dimension"], stats["vehicles"]
        ) != (ids, length):
            raise ValueError(
                f"{directory}: final tour differs from the earlier debugger-free execution"
            )
    depot = lambda i: i == 1 or i > stats["dimension"]
    empty = sum(depot(i) and depot(j) for i, j in zip(ids, ids[1:] + ids[:1]))
    return records, ids, penalty, length, empty


def run(root, binaries, cpu, reference):
    os.sched_setaffinity(0, {cpu})
    modes = {
        mode: (binaries / ("LKH" if mode == "original" else "LKH-vrptw-time")).resolve(
            strict=True
        )
        for mode in MODES
    }
    receipt = json.loads((binaries / "preparation.json").read_text())
    for exe in set(modes.values()):
        if sha(exe.read_bytes()) != receipt["binaries_sha256"][exe.name]:
            raise ValueError(f"Installed binary differs from its build receipt: {exe}")
    report = {
        "status": "in_progress",
        "cpu": cpu,
        "build": receipt["inputs"],
        "binary_sha256": receipt["binaries_sha256"],
        "cases": {},
    }
    try:
        for case in CASES:
            stats = json.loads((root / case / "input.json").read_text())
            observations = {}
            case_report = report["cases"][case] = {
                "inputs": stats,
                "modes": {},
                "comparisons": [],
            }
            for mode, exe in modes.items():
                prior = reference / case / mode if reference else None
                traces, tour, penalty, length, empty = run_one(
                    root / case / mode, exe, stats, prior
                )
                case_report["modes"][mode] = {
                    "rows": len(traces),
                    "tour_nodes": len(tour),
                    "final_penalty": penalty,
                    "final_integer_cost": length,
                    "empty_routes": empty,
                    "checked_native_tour": (root / case / mode / "final.tour").exists(),
                    "checked_prior_trace": bool(
                        prior and (prior / "solver.log").is_file()
                    ),
                    "checked_prior_tour": bool(
                        prior and (prior / "final.tour").is_file()
                    ),
                }
                if case == "directed-empty" and (
                    empty < 1
                    or stats["must_late_arcs"] < 1
                    or stats["asymmetric_pairs"] < 1
                ):
                    raise ValueError("Directed fixture missing required features")
                for prev, (ref, ids, ref_penalty, cost) in observations.items():
                    diff = [
                        {"before": a, "after": b}
                        for a, b in zip(ref, traces, strict=True)
                        if a != b
                    ]
                    tour_diff = sum(a != b for a, b in zip(ids, tour, strict=True))
                    comparison = {
                        "pair": [prev, mode],
                        "rows": len(traces),
                        "different_rows": len(diff),
                        "tour_nodes": len(tour),
                        "different_tour_nodes": tour_diff,
                        "mismatched_final_cost": cost != length,
                        "mismatched_final_penalty": ref_penalty != penalty,
                    }
                    case_report["comparisons"].append(comparison)
                    if diff or tour_diff or cost != length or ref_penalty != penalty:
                        comparison["trial_differences"] = diff
                        raise ValueError(
                            f"{case} {prev} vs {mode}: regression mismatch"
                        )
                observations[mode] = traces, tour, penalty, length
        report["status"] = "pass"
    except Exception as e:
        report.update(status="fail", error=str(e))
        raise
    finally:
        save_json(root / "results.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--reference",
        type=Path,
        help="Optional earlier, debugger-free execution directories",
    )
    parser.add_argument("--bin", type=Path, default=Path(".deps/lkh-3.0.10/bin"))
    parser.add_argument("--cpu", type=int, default=min(os.sched_getaffinity(0)))
    args = parser.parse_args()
    if args.action == "prepare":
        if not args.archive:
            parser.error("--archive is required for prepare")
        prepare(args.out, args.archive)
    else:
        run(args.out, args.bin, args.cpu, args.reference)


if __name__ == "__main__":
    main()
