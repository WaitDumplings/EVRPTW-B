"""Instrument a private LKH build and count actual CVRPTW penalty-path visits."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_SHA = "c7432409e359f4b712983be8670b351b20875de1be93d7b1a074c45e1744f910"
PATCH_SHA = "97f05de56cd147eff4d149c78129ad55431d62a1c34339fdab6643102178a0fd"
FIELDS = ("forward_steps", "reverse_steps", "depot_copy_lookups", "empty_route_steps",
          "pruned_lookups", "pruned_ge_m_lookups", "unpruned_m_lookups")
M = 10737418


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def edit(path, before, after):
    text = path.read_text()
    require(text.count(before) == 1, f"unexpected instrumentation site: {path}: {before}")
    path.write_text(text.replace(before, after))


def instrument(src):
    """Only the temporary instrumentation executable sees these counters and probe."""
    header = '\nextern unsigned long long Coverage[7];\nextern unsigned char *PrunedTime;\n'
    read = src / "ReadProblem.c"
    edit(read, '#include <errno.h>', '#include <errno.h>' + header)
    old = '    TravelTimeMatrix = malloc((size_t) Dim * Dim * sizeof(int));'
    edit(read, old, old + '\n    PrunedTime = calloc((size_t) Dim * Dim, 1);\n'
         '    if (!PrunedTime) eprintf("pruned arc coverage allocation failed");')
    old = '                        Ni->C[j] = M;\n                        /* Mirror'
    edit(read, old, '                        PrunedTime[(size_t)(i - 1) * Dim + j - 1] =\n'
         '                            *Time >= M ? 2 : 1;\n' + old)
    penalty = src / "Penalty_CVRPTW.c"
    edit(penalty, '#include "Segment.h"', '#include "Segment.h"' + header)
    old = '    if (lo->DepotId && hi->DepotId)\n        return 0;'
    edit(penalty, old, '    if (lo->DepotId && hi->DepotId) {\n'
         '        ++Coverage[3];\n        return 0;\n    }')
    old = '    return TravelTimeMatrix[(size_t) (i - 1) * Dim + j - 1];'
    edit(penalty, old, '''    size_t index = (size_t) (i - 1) * Dim + j - 1;
    int ticks = TravelTimeMatrix[index];
    if (lo->Id > Dim || hi->Id - DimensionSaved > Dim)
        ++Coverage[2];
    if (PrunedTime[index]) {
        ++Coverage[4];
        if (PrunedTime[index] == 2)
            ++Coverage[5];
    } else if (ticks == M) {
        ++Coverage[6];
    }
    return ticks;''')
    old = '            if (TravelTimeMatrix)\n                CostSum += TravelTime(N, NextN);'
    edit(penalty, old, '            if (TravelTimeMatrix) {\n'
         '                ++Coverage[Forward ? 0 : 1];\n'
         '                CostSum += TravelTime(N, NextN);\n            }')
    main = src / "LKHmain.c"
    old = 'int main(int argc, char * argv[])'
    edit(main, old, '''unsigned long long Coverage[7];
unsigned char *PrunedTime;

static void CoverageReport(void)
{
    printf("EVB_COVERAGE");
    for (int i = 0; i < 7; ++i)
        printf(" %llu", Coverage[i]);
    putchar('\\n');
}

/* Dedicated fixture only: construct the same physical circuit in both
   internal orientations. Call the real, patched penalty after ReadProblem. */
static void CoverageProbe(void)
{
    const int tour[] = {1, 5, 2, 3, 4};
    if (Dim != 4 || DimensionSaved != 5 || Salesmen != 2)
        eprintf("unexpected coverage probe dimensions");
    for (int m = 0; m < 5; ++m) {
        Node *from = &NodeSet[tour[m]];
        Node *to = &NodeSet[tour[(m + 1) % 5]];
        Node *upper = to + DimensionSaved;
        from->Suc = upper;
        upper->Pred = from;
        upper->Suc = to;
        to->Pred = upper;
    }
    for (int orientation = 0; orientation <= 1; ++orientation) {
        Reversed = orientation;
        CurrentPenalty = PLUS_INFINITY;
        CurrentGain = 0;
        GainType penalty = Penalty_CVRPTW();
        printf("EVB_PROBE orientation=%d penalty=" GainFormat "\\n", orientation, penalty);
    }
}

''' + old)
    edit(main, '    PrintStatistics();', """    printf("EVB_BEST");
    for (int k = 1; k <= DimensionSaved; ++k)
        printf(" %d", BestTour[k]);
    printf("\\n");
    PrintStatistics();""")
    edit(main, '    ReadProblem();', '''    ReadProblem();
    atexit(CoverageReport);
    if (getenv("EVB_LKH_COVERAGE_PROBE")) {
        CoverageProbe();
        return EXIT_SUCCESS;
    }''')


def build(archive, directory, out):
    patch = ROOT / "adapters/patches/lkh-3.0.10-vrptw-time.patch"
    require(sha(archive) == ARCHIVE_SHA and sha(patch) == PATCH_SHA, "pinned source hash mismatch")
    with tarfile.open(archive) as tf:
        tf.extractall(directory, filter="data")
    tree = directory / "LKH-3.0.10"
    with (out / "build.log").open("wb") as log:
        with patch.open("rb") as f:
            subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1"], cwd=tree,
                           stdin=f, stdout=log, stderr=subprocess.STDOUT, check=True)
        instrument(tree / "SRC")
        subprocess.run(["make", "-C", "SRC", "-j4", "CC=/usr/bin/cc", "LKH"], cwd=tree,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    return tree / "LKH"


def run(exe, dest, par, cpu, *, probe=False):
    env = os.environ.copy()
    env.pop("EVB_LKH_COVERAGE_PROBE", None)
    if probe:
        env["EVB_LKH_COVERAGE_PROBE"] = "1"
    with (dest / "solver.log").open("wb") as log:
        subprocess.run(["taskset", "-c", str(cpu), str(exe), par], env=env, cwd=dest,
                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, check=True)
    text = (dest / "solver.log").read_text()
    matches = re.findall(r"^EVB_COVERAGE((?: \d+){7})$", text, re.M)
    require(len(matches) == 1, f"missing coverage record in {dest}")
    return dict(zip(FIELDS, map(int, matches[0].split())))


def signature(log):
    return re.findall(r"^(?:[ *]*(\d+)|Run (\d+)): Cost = (\d+)_(\d+)", log.read_text(), re.M)


def natural(exe, src, out, name, cpu):
    manual = name == "manual"
    dst = out / name
    dst.mkdir()
    par, instance, tour, log = (("solver.par", "manual.vrp", "best.tour", "stdout.log")
                                if manual else ("test.par", "problem.vrp", "final.tour", "solver.log"))
    for f in (par, instance):
        shutil.copyfile(src / f, dst / f)
    counts = run(exe, dst, par, cpu)
    raw = signature(src / log)
    require(raw and raw == signature(dst / "solver.log"), f"instrumentation altered trials: {name}")
    observed = (dst / "solver.log").read_text()
    best = re.findall(r"^EVB_BEST((?: \d+)+)$", observed, re.M)
    require(len(best) == 1, f"missing internal final tour: {name}")
    ids = list(map(int, best[0].split()))
    if (src / "best-tour.json").is_file():
        memory = json.loads((src / "best-tour.json").read_text())
        require(ids == memory["raw"], f"instrumentation altered exact internal tour: {name}")
    if (src / tour).is_file():
        require((src / tour).read_text().split("TOUR_SECTION\n", 1)[1] ==
                (dst / tour).read_text().split("TOUR_SECTION\n", 1)[1],
                f"instrumentation altered final tour file: {name}")
    else:
        require((src / "best-tour.json").is_file(), f"missing formal reference tour: {name}")
    (dst / "coverage.json").write_text(json.dumps(counts, indent=2) + "\n")
    return counts


def probe(exe, out, cpu):
    dest = out / "known-circuit"
    dest.mkdir()
    # 1 is the depot, 5 its copy; route 1->5 is empty. Physical route
    # 5->2->3->4->1 traverses a short pruned, a long pruned, a benign
    # arc exactly equal to M, and a short unpruned arc, in that order.
    p = dest / "probe.vrp"
    p.write_text(f"""NAME: coverage-known-circuit
TYPE: CVRPTW
DIMENSION: 4
VEHICLES: 2
CAPACITY: 10
EDGE_WEIGHT_TYPE: EXPLICIT
EDGE_WEIGHT_FORMAT: FULL_MATRIX
EDGE_WEIGHT_SECTION
0 1 2 3
4 0 5 6
7 8 0 9
10 11 12 0
DEMAND_SECTION
1 0
2 1
3 1
4 1
DEPOT_SECTION
1
-1
TRAVEL_TIME_SECTION
0 10 1 1
1 0 {M+7} 1
1 1 0 {M}
3 1 1 0
TIME_WINDOW_SECTION
1 0 {10*M}
2 0 1
3 0 10
4 0 {5*M}
EOF
""")
    (dest / "probe.par").write_text("PROBLEM_FILE = probe.vrp\nMTSP_MIN_SIZE = 0\nSEED = 20261103\n")
    counts = run(exe, dest, "probe.par", cpu, probe=True)
    expected = dict(zip(FIELDS, (5, 5, 2, 2, 4, 2, 2)))
    require(counts == expected, f"probe counts {counts} differ from expected {expected}")
    raw = (dest / "solver.log").read_text()
    penalties = re.findall(r"^EVB_PROBE orientation=(\d+) penalty=(\d+)$", raw, re.M)
    require(penalties == [(str(i), str(3 * M - 4)) for i in range(2)], f"probe penalty: {penalties}")
    return counts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", required=True, type=Path, help="directory with completed parity and manual runs")
    p.add_argument("--archive", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path, help="new coverage evidence directory")
    p.add_argument("--cpu", default=min(os.sched_getaffinity(0)), type=int)
    args = p.parse_args()
    formal_dir = args.stage / "parity-capture"
    formal = json.loads((formal_dir / "results.json").read_text())
    require(formal["status"] == "pass", "formal best-tour comparison did not pass")
    binaries = Path(os.environ.get("EVB_DEPS", ROOT / ".deps")) / "lkh-3.0.10/bin"
    production = {name: sha(binaries / name) for name in ("LKH", "LKH-vrptw-time")}
    require(production == json.loads((binaries / "preparation.json").read_text())["binaries_sha256"],
            "production binary differs from its receipt")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="evb-lkh-coverage-") as tmp:
        exe = build(args.archive, Path(tmp), out)
        solver_counts = {"manual": natural(exe, args.stage / "manual", out, "manual", args.cpu)}
        for name in ("C101", "R101", "RC101", "directed-empty"):
            solver_counts[name] = natural(exe, formal_dir / name / "equal", out, name, args.cpu)
        known = probe(exe, out, args.cpu)
        data = {"archive_sha256": ARCHIVE_SHA, "patch_sha256": PATCH_SHA,
                "instrumented_binary_sha256": sha(exe), "cpu": args.cpu,
                "solver_cases": solver_counts, "known_circuit": known}
    require(production == {name: sha(binaries / name) for name in production},
            "production binary changed during coverage")
    data["production_sha256_before_and_after"] = production
    totals = {key: sum(c[key] for c in solver_counts.values()) for key in FIELDS}
    data["solver_totals"] = totals
    data["combined_totals"] = {k: v + known[k] for k, v in totals.items()}
    require(all(data["combined_totals"][key] > 0 for key in FIELDS[:5]), "unexecuted required branch")
    report = out / "coverage.json"
    report.write_text(json.dumps(data, indent=2) + "\n")
    print(report.read_text())


if __name__ == "__main__":
    main()
