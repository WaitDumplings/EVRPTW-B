"""Pinned worker. The parent releases the method once untimed preparation is complete."""
import argparse
from contextlib import ExitStack
import importlib
import json
import math
import operator
import os
import sys
import time
import traceback
from pathlib import Path
from runtime import MODULE, load_view, write_json


def native(x):
    if isinstance(x, dict):
        return {str(k): native(v) for k, v in x.items()}
    if isinstance(x, (tuple, list)):
        return [native(v) for v in x]
    if hasattr(x, "item"):
        return native(x.item())
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, Path):
        return str(x)
    return x


class Context:
    def __init__(self, args, started, stream):
        self.out, self.seed, self.view = args.out, args.seed, args.view
        self.started, self.deadline = started, started + args.seconds
        self.threads, self.stream = 1, stream

    def elapsed(self):
        return time.monotonic() - self.started

    def remaining(self):
        return max(0., self.deadline - time.monotonic() - 0.7)

    def _send(self, kind, payload):
        # Include conversion, full route extraction and encoding before stamping arrival.
        encoded = json.dumps(native(payload), allow_nan=False, separators=(",", ":"))
        stamp = time.monotonic()
        if kind == "candidate" and stamp > self.deadline:
            return False
        self.stream.write('{"kind":' + json.dumps(kind) + ',"t":' +
                          repr(stamp - self.started) + ',"data":' + encoded + '}\n')
        self.stream.flush()
        return True

    def emit(self, routes, **meta):
        normalized = [[operator.index(i) if not isinstance(i, bool) else i for i in route] for route in routes]
        return self._send("candidate", dict(routes=normalized, meta=meta))

    def info(self, **kwargs):
        return self._send("info", kwargs)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--method", required=True)
    p.add_argument("--view", required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--cpu", type=int, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--ready-fd", type=int)
    p.add_argument("--go-fd", type=int)
    p.add_argument("--probe", action="store_true")
    args = p.parse_args()
    os.sched_setaffinity(0, {args.cpu})
    task = load_view(args.input, args.view)
    if args.probe:
        from replay import station_probe
        x = station_probe(task)
        adapter = importlib.import_module(MODULE[args.method])
        actual = adapter.charge_probe(args.method, task, x["origin"], x["station"], x["arrival_kwh"])
        keys = ("energy_kwh", "charge_s", "departure_kwh")
        comparisons = {k: math.isclose(float(actual[k]), x[k], rel_tol=1e-9, abs_tol=1e-7) for k in keys}
        write_json(args.out / "station_probe.json", dict(expected=x, adapter=native(actual),
                   checks=comparisons, valid=all(comparisons.values())))
        return
    with ExitStack() as setup, (args.out / "events.jsonl").open("w", buffering=1) as stream:
        with os.fdopen(args.ready_fd, "w") as ready:
            receipt = {}
            try:
                adapter = importlib.import_module(MODULE[args.method])
                if args.method == "gurobi":
                    build_started = time.monotonic()
                    try:
                        prepared = setup.enter_context(adapter.prepare(task, args.view, args.seed))
                    finally:
                        receipt["model_build_s"] = time.monotonic() - build_started
            except Exception as err:
                receipt.update(reason=str(err), exception=type(err).__name__)
                print(json.dumps(receipt), file=ready)
                raise
            print(json.dumps(receipt), file=ready)
        with os.fdopen(args.go_fd) as channel:
            started = float(channel.readline())
        ctx = Context(args, started, stream)
        try:
            if args.method == "gurobi":
                adapter.solve_prepared(task, ctx, prepared)
            else:
                adapter.solve(args.method, task, ctx)
            ctx._send("finish", {"status": "returned"})
        except Exception as err:
            traceback.print_exc()
            ctx._send("finish", {"status": "error", "exception": type(err).__name__, "reason": str(err)})
            sys.exit(1)


if __name__ == "__main__":
    main()
