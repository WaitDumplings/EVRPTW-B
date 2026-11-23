"""Collect one separate initial solution after all timed jobs on its instance finish."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from initial_record import IDENTITY, INITIAL_PAIRS, SCHEMA, matches_initial, measure_routes
from runtime import file_hash, interpreter, load_view, one_thread_env, write_json


def handoff(result):
    """Distinguish observed search, a proven missing start, and an interrupted handoff."""
    if result.get("start_monotonic") is None:
        return "absent", "timed method never started"
    if result.get("candidates", 0):
        return "observed", "timed search delivered a candidate"
    info = result.get("adapter", {})
    if info.get("termination") == "deadline_before_launch":
        return "absent", "native construction was never launched"
    if result["method"] == "ortools":
        if (result.get("termination") or {}).get("status") == "returned":
            return "no_assignment", "first-solution callback delivered no assignment"
    if result["method"] == "vns-ts" and "runs" in info:
        if info["runs"] > 0:
            return "observed", "native run completed its construction"
        if (result.get("termination") or {}).get("status") == "returned":
            return "absent", "native solve returned before its first run"
    if info.get("vns_iterations", 0) or info.get("ts_iterations", 0) or info.get("iterations", 0) > 1:
        return "observed", "timed search advanced beyond construction"
    return "unconfirmed", "timed archive has no evidence that the first construction finished"


def collect(attempt, result):
    target = attempt / "initial_solution.json"
    if target.is_file():
        previous = json.loads(target.read_text())
        if not matches_initial(previous, result):
            raise ValueError(f"Existing initial record does not match the task: {target}")
        return
    path = Path(result["input"])
    if file_hash(path) != result["sha256"]:
        raise ValueError(f"Initial collection input hash mismatch: {path}")
    task = load_view(path, result["view"])
    info = task.info
    if info["instance_id"] != result["instance_id"]:
        raise ValueError("Initial collection instance ID mismatch")
    evidence, note = handoff(result)
    record = {k: result[k] for k in IDENTITY}
    record.update(schema=SCHEMA, city=info["city"], split=info["split"], n=info["customer_count"],
                  acquisition="posthoc_reproduction", timed_handoff=evidence, reason=note,
                  routes=None, vehicles=None, charging_visits=None, distance_m=None,
                  feasible=None, violations=None, replay_error=None, construction_s=None)
    if evidence == "absent":
        record["status"] = "no_initial_solution"
    else:
        from adapters.non_learning.initial import construct
        with tempfile.TemporaryDirectory(dir=attempt, prefix=".initial-") as temp:
            first = construct(result["method"], task, result["seed"], Path(temp))
        duration = first["construction_s"]
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("Constructor returned an invalid duration")
        record["construction_s"] = duration
        if evidence == "no_assignment":
            record["status"] = "no_initial_solution"
            if first["routes"] is not None:
                record["reproduction"] = dict(routes=first["routes"], **measure_routes(task, first["routes"]))
        elif first["routes"] is None:
            if evidence == "observed":
                raise ValueError("Construction returned no routes for a run that entered search")
            record.update(status="no_initial_solution", reason="constructor returned no solution")
        else:
            metrics = measure_routes(task, first["routes"])
            if evidence == "unconfirmed":
                # Deadline exits can occur on either side of the first raw construction.
                # Preserve the reconstruction without attributing it to the timed run.
                record.update(status="unknown", reproduction=dict(routes=first["routes"], **metrics))
            else:
                record.update(status="recorded", routes=first["routes"], **metrics)
    write_json(target, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    attempt = args.attempt.resolve(strict=True)
    result = json.loads((attempt / "result.json").read_text())
    if (result["method"], result["view"]) not in INITIAL_PAIRS:
        parser.error("this method and view have no initial-solution record")
    if not args.worker and handoff(result)[0] != "absent":
        exe = interpreter(result["method"])
        os.execve(exe, [exe, str(Path(__file__).resolve()), "--attempt", str(attempt), "--worker"],
                  one_thread_env())
    # Atomic publication and safe simultaneous resume without adding a lock file.
    directory = os.open(attempt, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        collect(attempt, result)
    finally:
        os.close(directory)


if __name__ == "__main__":
    main()
