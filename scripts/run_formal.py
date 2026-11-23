#!/usr/bin/env python3
"""Schedule the public test catalog on distinct physical cores, then collect constructions."""
import argparse
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
import fcntl
from itertools import groupby
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from initial_record import INITIAL_PAIRS, matches_initial
from runtime import COMBINATIONS, DEFAULT_SEED, READER, cpu_topology, file_hash, one_thread_env, write_json
from scripts.run_test1 import ERRORS, completed_attempt as find_attempt

SPLITS = ("test1", "xs", "test2")
SIZES = (100, 500, 1000)
SECONDS = 1800


def planned_pairs(row, gurobi):
    return [(m, v) for m, v in COMBINATIONS if m != "gurobi" or (gurobi and row["n"] == 100)]


def completed_attempt(out, row, method, view, cfg):
    return find_attempt(out, row, method, {"view": view, "seconds": cfg["seconds"], "seed": cfg["seed"]})


def phase(row):
    return (row["split"] == "test2", row["n"])


def parse_ranges(items):
    ranges = {}
    for entry in items:
        if len(entry) not in (2, 3):
            raise ValueError("--test2-range takes [SIZE] FIRST LAST")
        n = entry[0] if len(entry) == 3 else None
        first, last = entry[-2:]
        if (n is not None and n not in SIZES) or n in ranges or not 1 <= first <= last:
            raise ValueError(f"invalid or duplicate test2 range: {entry}")
        ranges[n] = (first, last)
    return ranges


def select_inputs(root, cities, splits, ranges):
    import pyarrow.parquet as pq

    fields = ["city", "split", "n", "instance_id", "path", "bytes", "sha256"]
    table = pq.read_table(root / "index.parquet", columns=fields).to_pylist()
    unknown = set(cities or ()) - {row["city"] for row in table}
    if unknown:
        raise ValueError(f"unknown cities in the public index: {sorted(unknown)}")
    selected = [row for row in table if row["split"] in splits and (not cities or row["city"] in cities)]
    selected.sort(key=lambda row: (*phase(row), row["instance_id"]))
    if ranges and not any(r["split"] == "test2" for r in selected):
        raise ValueError("--test2-range requires selected test2 instances")
    result = []
    for (test2, size), grouped in groupby(selected, key=phase):
        block = list(grouped)
        if test2 and ranges:
            interval = ranges.get(size, ranges.get(None))
            if interval is None:
                continue
            first, last = interval
            if last > len(block):
                raise ValueError(f"test2 Cus{size} has {len(block)} selected instances, requested {first}..{last}")
            block = block[first - 1:last]
        result.extend(block)
    if not result or len({r["instance_id"] for r in result}) != len(result):
        raise ValueError("empty selection or duplicate instance IDs")
    for row in result:
        city, split, n, ident = (row[k] for k in ("city", "split", "n", "instance_id"))
        if not (isinstance(ident, str) and ident and ident.isascii() and ident.isalnum()
                and isinstance(city, str) and city and Path(city).name == city and city not in (".", "..")):
            raise ValueError(f"invalid catalog identity: {row}")
        expected = Path("instances", city, split, f"Cus{n}", f"{ident}.parquet")
        if (n not in ((2000,) if split == "xs" else SIZES) or row["path"] != str(expected)
                or (root / expected).stat().st_size != row["bytes"]):
            raise ValueError(f"catalog path, size class or byte count mismatch: {expected}")
    return result


def make_manifest(args):
    root = args.data_root
    index_hash = file_hash(root / "index.parquet")
    release = root / "release.json"
    if release.exists() and json.loads(release.read_text())["index_sha256"] != index_hash:
        raise ValueError("index.parquet does not match release.json")
    rows = select_inputs(root, args.cities, args.splits, parse_ranges(args.test2_range))
    return dict(format_version=1, index_sha256=index_hash, seconds=SECONDS, threads=1,
                seed=args.seed, gurobi=args.gurobi, inputs=rows)


def show_plan(cfg, list_tasks, output):
    rows = cfg["inputs"]
    counts = Counter((r["split"], r["n"]) for r in rows)
    total = sum(len(planned_pairs(row, cfg["gurobi"])) for row in rows)
    print(f"# instances={len(rows)} tasks={total} core_hours={total * cfg['seconds'] / 3600:g}", file=output)
    for (split, size), n in counts.items():
        print(f"# {split} Cus{size} instances={n}", file=output)
    if list_tasks:
        print("city\tsplit\tn\tinstance_id\tmethod\tview", file=output)
        for r in rows:
            for method, view in planned_pairs(r, cfg["gurobi"]):
                print(r["city"], r["split"], r["n"], r["instance_id"], method, view,
                      sep="\t", file=output)


def reader_environment(explicit, root, first):
    """Check the actual public file in the controller environment before dispatch."""
    env = one_thread_env()
    chosen = explicit or env.get("EVRPTWD_READER")
    candidates = ([Path(chosen).expanduser()] if chosen else
                  [READER, ROOT.parent / "EVRPTW-D-HF-two-part"])
    errors = []
    for path in dict.fromkeys(p.resolve() for p in candidates):
        env["EVRPTWD_READER"] = str(path)
        probe = subprocess.run([sys.executable, "-c",
                                "from runtime import load_view; import sys; load_view(sys.argv[1], 'cvrp')",
                                str(root / first["path"])], cwd=ROOT, env=env, text=True, capture_output=True)
        if probe.returncode == 0:
            return env
        errors.append(f"{path}: {probe.stderr.strip()}")
    raise ValueError("a public-v2 compatible reader is required (--reader or EVRPTWD_READER)\n" + "\n".join(errors))


def run_instance(row, cpu, args, cfg, env, lock_fds):
    outcomes = Counter()
    initial = []

    def invoke(command):
        return subprocess.run(["taskset", "-c", str(cpu), sys.executable, *command],
                              cwd=ROOT, env=env, pass_fds=lock_fds).returncode

    for method, view in planned_pairs(row, cfg["gurobi"]):
        attempt = completed_attempt(args.out, row, method, view, cfg)
        if attempt is None or (args.retry_errors and attempt[1] in ERRORS):
            code = invoke([str(ROOT / "bench.py"), "run", "--input", str(args.data_root / row["path"]),
                           "--solver", method, "--view", view, "--cpu", str(cpu), "--threads", "1",
                           "--seconds", str(cfg["seconds"]), "--seed", str(cfg["seed"]), "--out", str(args.out),
                           "--expect-sha256", row["sha256"], "--expect-id", row["instance_id"]])
            current = completed_attempt(args.out, row, method, view, cfg)
            if code not in (0, 1) or current is None or current == attempt:
                raise RuntimeError(f"{row['instance_id']} {method}/{view}: exit {code}, missing new terminal result")
            attempt = current
        folder, status = attempt
        outcomes[status] += 1
        if (method, view) in INITIAL_PAIRS:
            initial.append(folder)

    # This slot retains its exclusive physical core throughout the post-task processes.
    for folder in initial:
        record = folder / "initial_solution.json"
        result = json.loads((folder / "result.json").read_text())
        if record.exists() and matches_initial(json.loads(record.read_text()), result):
            continue
        code = invoke([str(ROOT / "scripts/collect_initial.py"), "--attempt", str(folder)])
        if code or not record.is_file() or not matches_initial(json.loads(record.read_text()), result):
            raise RuntimeError(f"initial collection did not produce a matching record: {folder}, exit {code}")
    return outcomes


@contextmanager
def output_lock(out, cfg):
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".formal.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"formal scheduler already active in {out}") from exc
        manifest = out / "manifest.json"
        if manifest.is_file():
            if json.loads(manifest.read_text()) != cfg:
                raise ValueError(f"output manifest differs from the requested panel or protocol: {manifest}")
        else:
            write_json(manifest, cfg)
        yield lock.fileno()


@contextmanager
def physical_leases(representatives, jobs, lock_root):
    """Share physical-core reservations between this user's concurrent formal panels."""
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with ExitStack() as stack:
        cores = {}
        for (socket, physical), cpu in representatives.items():
            lock = stack.enter_context((lock_root / f"{socket}-{physical}").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            cores[cpu] = lock.fileno()
            if len(cores) == jobs:
                break
        if not cores:
            raise ValueError("all permitted physical cores are reserved by other formal panels")
        yield cores


def schedule(args, cfg, env, cores, lock_fd):
    totals = Counter()
    with ThreadPoolExecutor(max_workers=len(cores)) as pool:
        for key, rows in groupby(cfg["inputs"], key=phase):
            print(f"Starting {'test2' if key[0] else 'test1/xs'} Cus{key[1]}", flush=True)
            free = deque(cores)
            pending = {}
            for row in rows:
                if not free:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for job in done:
                        free.append(pending.pop(job))
                        totals.update(job.result())
                cpu = free.popleft()
                job = pool.submit(run_instance, row, cpu, args, cfg, env, (lock_fd, cores[cpu]))
                pending[job] = cpu
            for job in pending:
                totals.update(job.result())
    return totals


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True, help="public release containing index.parquet and instances/")
    p.add_argument("--out", type=Path, help="formal output directory; required to execute")
    p.add_argument("--cities", nargs="+", help="default: all cities in the release")
    p.add_argument("--splits", nargs="+", choices=SPLITS, default=SPLITS)
    p.add_argument("--test2-range", type=int, nargs="+", action="append", default=[], metavar="INT",
                   help="FIRST LAST for every test2 size, or SIZE FIRST LAST for a specific size; inclusive, one-based")
    p.add_argument("--max-jobs", type=int, help="single-core slots; default: half the available physical cores")
    p.add_argument("--gurobi", action="store_true", help="include all three Gurobi views on Cus100")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--reader", type=Path, help="public-v2 EVRPTW-D reader checkout")
    p.add_argument("--retry-errors", action="store_true", help="retry technical errors once; retain all earlier attempts")
    p.add_argument("--plan-only", action="store_true", help="print all intended tasks and core-hours without executing")
    args = p.parse_args(argv)
    physical = {}
    for cpu, key in cpu_topology().items():
        physical.setdefault(key, cpu)
    jobs = max(1, len(physical) // 2) if args.max_jobs is None else args.max_jobs
    if not 1 <= jobs <= len(physical):
        p.error(f"--max-jobs must be between 1 and {len(physical)} available physical cores")
    if not args.plan_only and args.out is None:
        p.error("--out is required to execute")
    args.data_root = args.data_root.resolve()
    try:
        cfg = make_manifest(args)
        show_plan(cfg, args.plan_only, sys.stdout)
        if args.plan_only:
            return 0
        args.out = args.out.resolve()
        with output_lock(args.out, cfg) as lock_fd:
            env = reader_environment(args.reader, args.data_root, cfg["inputs"][0])
            locks = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / f"evrptwb-cores-{os.getuid()}"
            with physical_leases(physical, jobs, locks) as cores:
                print(f"Physical core slots (logical CPU representatives): {list(cores)}", flush=True)
                totals = schedule(args, cfg, env, cores, lock_fd)
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        p.exit(1, f"{p.prog}: {exc}\n")
    print(f"Final outcomes: {dict(totals)}")
    return int(any(totals[key] for key in ERRORS))


if __name__ == "__main__":
    sys.exit(main())
