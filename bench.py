#!/usr/bin/env python3
"""Run a pinned solver once, or schedule independent instances on physical cores."""
import argparse
import concurrent.futures
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from replay import InvalidRoute, evaluate
from runtime import (COMBINATIONS, DEFAULT_SEED, ROOT, cpu_topology, file_hash, interpreter,
                     load_view, one_thread_env, write_json)

CHECKPOINTS = (60, 300, 900, 1800)


def attempt_directory(root):
    root.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        dest = root / f"attempt-{index:03d}"
        try:
            dest.mkdir()
            return dest
        except FileExistsError:
            index += 1


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def launch(command, out, seconds):
    ready, ready_w = os.pipe()
    go_r, go = os.pipe()
    proc = None
    start = None
    watchdog = False
    bootstrap = time.monotonic()
    try:
        with (out / "solver.log").open("wb") as log:
            proc = subprocess.Popen([*command, "--ready-fd", str(ready_w), "--go-fd", str(go_r)],
                                    pass_fds=(ready_w, go_r), start_new_session=True,
                                    env=one_thread_env(), stdout=log, stderr=subprocess.STDOUT)
            os.close(ready_w); ready_w = -1
            os.close(go_r); go_r = -1
            # The common reader runs before the method budget. EOF reports a failed child.
            if os.read(ready, 1) != b"R":
                raise RuntimeError("worker could not load the common input (see solver.log)")
            start = time.monotonic()
            os.write(go, f"{start!r}\n".encode())
            os.close(go); go = -1
            try:
                proc.wait(timeout=max(0., start + seconds - time.monotonic()))
            except subprocess.TimeoutExpired:
                watchdog = True
                kill_group(proc.pid)
                proc.wait()
            observed = time.monotonic() - start
        return dict(start_monotonic=start, bootstrap_s=start-bootstrap, observed_worker_exit_s=observed,
                    watchdog=watchdog, returncode=proc.returncode)
    finally:
        for fd in (ready, ready_w, go_r, go):
            if fd >= 0:
                os.close(fd)
        if proc is not None:
            kill_group(proc.pid)
            proc.wait()


def inspect_events(path, task, seconds):
    checkpoints = sorted({t for t in CHECKPOINTS if t <= seconds} | {seconds})
    selected, rejected, metadata = [], [], {}
    feasible_times = []
    bound_history = []
    finish = None
    candidates = 0
    if path.is_file():
        with path.open() as f:
            for line in f:
                if not line.endswith("\n"):  # SIGKILL may interrupt the last write.
                    break
                event = json.loads(line)
                kind, t, data = event["kind"], event["t"], event["data"]
                if kind == "finish":
                    finish = dict(elapsed_s=t, **data)
                elif kind == "info":
                    if 0 <= t <= seconds:
                        metadata.update(data)
                bound = (data.get("meta", {}) if kind == "candidate" else data).get("lower_bound_m")
                if 0 <= t <= seconds and bound is not None:
                    bound_history.append((t, bound))
                if kind == "candidate" and 0 <= t <= seconds:
                    candidates += 1
                    try:
                        check = evaluate(task, data["routes"])
                    except InvalidRoute as error:
                        rejected.append(dict(elapsed_s=t, reason=str(error)))
                        continue
                    feasible_times.append(t)
                    if not selected or check["distance_m"] < selected[-1]["distance_m"]:
                        selected.append(dict(elapsed_s=t, routes=data["routes"],
                                             meta=data["meta"], **check))
    snapshots = []
    for deadline in checkpoints:
        best = next((r for r in reversed(selected) if r["elapsed_s"] <= deadline), None)
        snapshot = dict(at_s=deadline, best=best)
        if "bound_scope" in metadata:
            snapshot.update(bound_scope=metadata["bound_scope"],
                            lower_bound_m=max((b for t, b in bound_history if t <= deadline), default=None),
                            upper_bound_m=best["distance_m"] if best else None)
        snapshots.append(snapshot)
    return (selected[-1] if selected else None, min(feasible_times, default=None), snapshots,
            metadata, rejected, candidates, finish)


def execute(path, method, view, args, cpu):
    path = path.resolve(strict=True)
    before = time.monotonic()
    task = load_view(path, view)
    digest = file_hash(path)
    identifier = task.info["instance_id"]
    if (args.expect_sha256 and digest != args.expect_sha256) or (args.expect_id and identifier != args.expect_id):
        raise ValueError(f"input identity mismatch: {identifier} {digest}")
    ordinal = COMBINATIONS.index((method, view)) + 1
    dest = attempt_directory(args.out.resolve() / identifier / f"{ordinal:02d}-{method}-{view}")
    package, physical = cpu_topology()[cpu]
    result = dict(instance_id=identifier, input=str(path), sha256=digest, method=method, view=view,
                  seconds=args.seconds, threads=1, seed=args.seed, cpu=cpu, socket=package,
                  physical_core=physical, interpreter=interpreter(method), input_prepare_s=time.monotonic()-before)
    command = [result["interpreter"], str(ROOT/"worker.py"), "--method", method, "--view", view,
               "--input", str(path), "--cpu", str(cpu), "--seed", str(args.seed), "--seconds",
               str(args.seconds), "--out", str(dest)]
    try:
        run = launch(command, dest, args.seconds)
    except (OSError, RuntimeError) as e:
        result.update(status="setup_error", reason=str(e))
        write_json(dest/"result.json", result)
        return dest, result
    replay_started = time.monotonic()
    best, first, checkpoints, metadata, rejected, n, finish = inspect_events(dest/"events.jsonl", task, args.seconds)
    result.update(**run, first_feasible_s=first, candidates=n, invalid_candidates=len(rejected),
                  adapter=metadata, termination=finish)
    # A killed worker charged the full budget. Also report actual process exit latency.
    result["wall_s"] = args.seconds if run["watchdog"] else (
        finish["elapsed_s"] if finish else run["observed_worker_exit_s"])
    write_json(dest/"checkpoints.json", checkpoints)
    if rejected:
        write_json(dest/"rejected.json", rejected)
    if best:
        write_json(dest/"routes.json", dict(instance_id=identifier, sha256=digest, method=method, view=view,
                                            elapsed_s=best["elapsed_s"], routes=best["routes"]))
        replay = evaluate(task, best["routes"], trace=True)
        write_json(dest/"replay.json", replay)
        result.update(status="pass", best_s=best["elapsed_s"], distance_m=replay["distance_m"],
                      vehicles=replay["vehicles"], charging_visits=replay["charging_visits"],
                      best_metadata=best["meta"])
    elif finish and finish.get("exception") == "NotImplementedError":
        result.update(status="incompatible", reason=finish["reason"])
    elif finish and finish.get("status") == "error":
        reason = finish["reason"]
        status = "license_blocked" if ("license" in reason.lower() or "licence" in reason.lower()) else "execution_error"
        result.update(status=status, reason=reason)
    elif rejected:
        result.update(status="replay_failed", reason=rejected[0]["reason"])
    elif run["watchdog"] or (finish and finish.get("status") == "returned"):
        result.update(status="no_feasible_within_budget", reason="no complete feasible route received within budget")
    else:
        result.update(status="execution_error", reason=f"worker exit {run['returncode']}; see solver.log")
    if method == "gurobi" and "bound_scope" in metadata:
        metadata["lower_bound_m"] = checkpoints[-1]["lower_bound_m"]
        if best:
            metadata["upper_bound_m"] = best["distance_m"]
        if run["watchdog"] and not metadata.get("solver_status"):
            metadata["solver_status"] = "external_wall_time_limit"
    result["replay_s"] = time.monotonic() - replay_started
    if best and view == "evrptw" and not best["charging_visits"]:
        probe_start = time.monotonic()
        probe_path = dest/"station_probe.json"
        try:
            with (dest/"probe.log").open("wb") as log:
                with subprocess.Popen([*command, "--probe"], stdout=log, stderr=subprocess.STDOUT,
                                      env=one_thread_env(), start_new_session=True) as process:
                    try:
                        code = process.wait(timeout=120)
                    finally:
                        kill_group(process.pid)
            if code:
                raise RuntimeError(f"station probe exited with code {code}, see probe.log")
            if not json.loads(probe_path.read_text()).get("valid"):
                raise ValueError("station resource mismatch, see station_probe.json")
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            result.update(status="station_probe_failed", reason=str(error))
        result["station_probe_s"] = time.monotonic() - probe_start
    write_json(dest/"result.json", result)
    return dest, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("run", "batch"):
        p = sub.add_parser(mode)
        if mode == "run":
            p.add_argument("--input", type=Path, required=True)
            p.add_argument("--cpu", type=int)
        else:
            p.add_argument("--input", type=Path, nargs="+", required=True)
            p.add_argument("--jobs", type=int, default=1, help="parallel instances on distinct physical cores")
        methods = sorted({m for m, _ in COMBINATIONS})
        p.add_argument("--solver", choices=methods+(["all"] if mode=="batch" else []),
                       required=mode=="run", default="all")
        p.add_argument("--view", choices=["cvrp","vrptw","evrptw"])
        p.add_argument("--threads", type=int, choices=[1], default=1)
        p.add_argument("--seconds", type=float, default=60 if mode=="run" else 1800)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--seed", type=int, default=DEFAULT_SEED)
        p.add_argument("--expect-sha256")
        p.add_argument("--expect-id")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")
    pairs = COMBINATIONS if args.solver=="all" else [(args.solver,args.view)]
    if args.solver == "all" and args.view:
        pairs = [(m,v) for m,v in pairs if v==args.view]
    if any(pair not in COMBINATIONS for pair in pairs):
        parser.error("unsupported solver/view combination")
    topology = cpu_topology()
    representatives = {}
    for cpu, key in topology.items():
        representatives.setdefault(key,cpu)
    cores = list(representatives.values())

    def run_inputs(cpu, paths):
        # Linux pid 0 sets the calling thread's affinity. Its solver children
        # inherit the same assigned core during input loading and replay.
        os.sched_setaffinity(0, {cpu})
        success = True
        for path in paths:
            for method, view in pairs:
                dest, result = execute(path, method, view, args, cpu)
                print(f"{result['status']} {dest}", flush=True)
                success &= result["status"] == "pass"
        return success
    if args.mode == "run":
        cpu = cores[0] if args.cpu is None else args.cpu
        if cpu not in topology:
            parser.error("CPU is outside the process affinity mask")
        return 0 if run_inputs(cpu,[args.input]) else 1
    if not 1 <= args.jobs <= len(cores):
        parser.error(f"--jobs must be between 1 and {len(cores)}")
    if len({p.resolve() for p in args.input}) != len(args.input):
        parser.error("duplicate instance in batch input")
    jobs = min(args.jobs, len(args.input))
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_inputs,cores[i],args.input[i::jobs]) for i in range(jobs)]
        outcomes = [f.result() for f in futures]
    return 0 if all(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
