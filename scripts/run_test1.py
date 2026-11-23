#!/usr/bin/env python3
"""Run the published Test1 cases one solver at a time, with two size-dependent pools."""
import argparse
from collections import Counter
import fcntl
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bench import checkpoint_times
from runtime import COMBINATIONS, DEFAULT_SEED, cpu_topology, file_hash, write_json

POOLS = (("Cus100+500", (100, 500)), ("Cus1000", (1000,)))
MEASURED = {"pass", "no_feasible_within_budget"}
ERRORS = {"setup_error", "incompatible", "license_blocked", "execution_error", "replay_failed",
          "station_probe_failed"}


def read_inputs(root, cities):
    import pyarrow.parquet as pq

    records = []
    excluded = Counter()
    for city in sorted(cities):
        slots = pq.read_table(root / "reports" / city / "slots.parquet",
                              filters=[("split", "=", "test1")]).to_pylist()
        if not slots:
            raise ValueError(f"{city} has no Test1 slots")
        for slot in slots:
            if slot["status"] != "protocol_certified":
                excluded[(city, slot["status"])] += 1
                continue
            row = {k: slot[k] for k in ("city", "n", "slot", "instance_id", "sha256", "path")}
            expected = Path("instances", city, "test1", f"Cus{row['n']}", f"{row['instance_id']}.parquet")
            if row["city"] != city or row["n"] not in (100, 500, 1000) or Path(row["path"]) != expected:
                raise ValueError(f"unexpected Test1 slot: {row}")
            if file_hash(root / expected) != row["sha256"]:
                raise ValueError(f"published input SHA-256 mismatch: {expected}")
            records.append(row)
    if excluded:
        print("Uncertified source slots:", dict(excluded), flush=True)
    if not records or len({r["instance_id"] for r in records}) != len(records):
        raise ValueError("empty Test1 panel or duplicate instance IDs")
    return sorted(records, key=lambda r: (r["slot"], r["n"], r["city"]))


def completed_attempts(out, row, method, cfg, *, assess=None):
    """Yield terminal attempts newest first. Unpublished or incomplete attempts do not count."""
    view = cfg["view"]
    i = COMBINATIONS.index((method, view)) + 1
    folder = out / row["instance_id"] / f"{i:02d}-{method}-{view}"
    files = sorted(folder.glob("attempt-*/result.json"),
                   key=lambda f: int(f.parent.name.split("-")[-1]), reverse=True)
    for file in files:
        r = json.loads(file.read_text())
        expected = {"sha256": row["sha256"], "instance_id": row["instance_id"], "method": method,
                    "view": view, "seed": cfg["seed"], "seconds": cfg["seconds"], "threads": 1}
        if any(r.get(k) != v for k, v in expected.items()):
            raise ValueError(f"existing result has a different input or configuration: {file}")
        if r.get("checkpoint_times_s") != checkpoint_times(cfg["seconds"]):
            continue  # Preserve older attempts and run the current timing protocol.
        status = r["status"]
        if status not in MEASURED | ERRORS:
            raise ValueError(f"unknown outcome in {file}: {status}")
        needed = []
        if status in MEASURED:
            needed = ["checkpoints.json"]
        if status == "pass":
            needed += ["routes.json", "replay.json"]
            if view == "evrptw" and r.get("charging_visits") == 0:
                needed += ["station_probe.json"]
        if not all((file.parent / name).is_file() for name in needed):
            continue
        if status in MEASURED:
            try:
                checkpoints = json.loads((file.parent / "checkpoints.json").read_text())
                recorded_times = [point["at_s"] for point in checkpoints]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if recorded_times != r["checkpoint_times_s"]:
                continue
            if assess is not None and assess(r):
                status = "execution_error"
        yield file.parent, status


def completed_attempt(out, row, method, cfg):
    return next(completed_attempts(out, row, method, cfg), None)


def completed(out, row, method, cfg):
    attempt = completed_attempt(out, row, method, cfg)
    return attempt[1] if attempt else None


def register_methods(out, view, methods):
    path = out / "run-plan.json"
    registered = set(json.loads(path.read_text())["methods"]) if path.exists() else set()
    registered.update(methods)
    order = [m for m, v in COMBINATIONS if v == view]
    if registered.difference(order):
        raise ValueError("run-plan contains methods for a different view")
    write_json(path, {"methods": [m for m in order if m in registered]})


def run_phase(args, lock, cfg, method, label, records, jobs):
    prior = [(row, completed(args.out, row, method, cfg)) for row in records]
    pending = [row for row, status in prior if status is None or (args.retry_errors and status in ERRORS)]
    print(f"{method}/{cfg['view']} {label}: {len(pending)} to run, {len(records)} planned, "
          f"{jobs} parallel instances", flush=True)
    if pending:
        command = [sys.executable, str(ROOT / "bench.py"), "batch", "--solver", method, "--view", cfg["view"],
                   "--seconds", str(cfg["seconds"]), "--threads", "1", "--seed", str(cfg["seed"]),
                   "--out", str(args.out), "--jobs", str(min(jobs, len(pending))), "--input",
                   *(str(args.data_root / row["path"]) for row in pending)]
        # Keep the output lock while the batch is alive even if its launcher is interrupted.
        process = subprocess.run(command, pass_fds=(lock.fileno(),))
        if process.returncode not in (0, 1):
            raise RuntimeError(f"batch exited with {process.returncode}; restart using the same --out")
    counts = Counter(completed(args.out, row, method, cfg) for row in records)
    if counts.get(None):
        raise RuntimeError(f"{counts[None]} missing outcomes in {method}/{label}; see the batch log")
    print(f"{method}/{cfg['view']} {label}: {dict(counts)}", flush=True)
    return counts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True, help="EVRPTW-D output with reports/ and instances/")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--cities", nargs="+", default=["chicago", "houston"])
    p.add_argument("--view", choices=["cvrp", "vrptw", "evrptw"], default="cvrp")
    p.add_argument("--solver", default="all", choices=["all", *dict.fromkeys(m for m, _ in COMBINATIONS)])
    p.add_argument("--small-jobs", type=int, default=32)
    p.add_argument("--large-jobs", type=int, default=20)
    p.add_argument("--seconds", type=float, default=1800)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--plan-only", action="store_true", help="validate inputs and show the plan; start no jobs")
    p.add_argument("--retry-errors", action="store_true", help="retry recorded technical errors once per invocation")
    args = p.parse_args()
    if len(set(args.cities)) != len(args.cities) or any(Path(c).name != c for c in args.cities):
        p.error("--cities must contain distinct city names")
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        p.error("--seconds must be finite and positive")
    cores = len(set(cpu_topology().values()))
    if not 1 <= args.small_jobs <= cores or not 1 <= args.large_jobs <= cores:
        p.error(f"both pool sizes must be between 1 and {cores} physical cores")
    methods = [m for m, v in COMBINATIONS if v == args.view and args.solver in ("all", m)]
    if not methods:
        p.error(f"unsupported combination: {args.solver}/{args.view}")
    args.data_root = args.data_root.resolve(strict=True)
    args.out = args.out.resolve()
    rows = read_inputs(args.data_root, args.cities)
    cfg = dict(split="test1", view=args.view, seconds=args.seconds, threads=1,
               seed=args.seed, cities=sorted(args.cities), inputs=rows)
    print("Certified input counts:", dict(sorted(Counter((r["city"], r["n"]) for r in rows).items())),
          flush=True)
    print(f"Solvers in order: {', '.join(methods)}. {len(rows) * len(methods)} runs.", flush=True)
    if args.plan_only:
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / ".test1.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            p.error("another Test1 run is using this output directory")
        manifest = args.out / "manifest.json"
        if manifest.exists():
            if json.loads(manifest.read_text()) != cfg:
                p.error("output manifest has different inputs, view, budget or seed; use a different --out")
        else:
            write_json(manifest, cfg)
        register_methods(args.out, args.view, methods)
        totals = Counter()
        for method in methods:
            for (label, sizes), jobs in zip(POOLS, (args.small_jobs, args.large_jobs)):
                pool = [r for r in rows if r["n"] in sizes]
                totals.update(run_phase(args, lock, cfg, method, label, pool, jobs))
        print("Final outcomes:", dict(totals), flush=True)
        return 1 if any(totals[s] for s in ERRORS) else 0


if __name__ == "__main__":
    sys.exit(main())
