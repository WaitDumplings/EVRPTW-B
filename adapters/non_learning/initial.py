"""Reproduce the very first construction in an isolated, post-run process."""
import json
from pathlib import Path
import subprocess
import sys
import time


def _ortools(task, seed):
    from .classical import _ortools_model, _ortools_parameters

    routing, routes_at = _ortools_model(task, seed)
    params = _ortools_parameters(seed)
    # Stop at the first assignment, before applying local search operators.
    params.solution_limit = 1
    started = time.monotonic()
    answer = routing.SolveWithParameters(params)
    elapsed = time.monotonic() - started
    return dict(routes=routes_at(answer.Value) if answer is not None else None,
                construction_s=elapsed)


def _vns(task, seed, workdir):
    from . import electric

    cfg, matrices = electric._inputs(task)
    path = Path(workdir) / "vns-construction-input.txt"
    electric._native_input(path, cfg, matrices)
    cmd = [str(electric.VNS), str(path), "--construct-only", *electric.VNS_SEARCH_ARGS,
           "--seed", str(seed)]
    return json.loads(subprocess.check_output(cmd, text=True))


def _routingblocks(task, seed):
    from . import electric

    cfg, matrices = electric._inputs(task)
    api, (py, cpp, evaluation) = electric._routingblocks(cfg, matrices)
    params = api.ALNSParams(**json.loads((api.VENDOR / "examples/evrptw/config.json").read_text()))
    result = None

    class ConstructionComplete(Exception):
        pass

    def first(solution, stage, iteration):
        nonlocal result
        if stage != "random_construction":
            raise RuntimeError(f"Unexpected first RoutingBlocks stage: {stage}")
        elapsed = time.monotonic() - started
        routes = [[node.vertex_id for node in route] for route in solution if not route.empty]
        result = dict(routes=routes, construction_s=elapsed)
        raise ConstructionComplete

    # The pinned bridge exposes each random construction immediately before local
    # search. Stop there on the very first start, including an infeasible one.
    solver = api.WeightedALNS(evaluation, py, cpp, params, seed, cfg,
                              time.monotonic(), float("inf"), first)
    started = time.monotonic()
    try:
        solver.run(float("inf"), sys.maxsize, sys.maxsize)
    except ConstructionComplete:
        return result
    return dict(routes=None, construction_s=time.monotonic() - started)


def construct(method, task, seed, workdir):
    """Return ordered, depot-closed vehicle routes, including infeasible constructions."""
    view = task.info["problem_type"]
    if method == "ortools" and view in ("cvrp", "vrptw"):
        return _ortools(task, seed)
    if method == "vns-ts" and view == "evrptw":
        return _vns(task, seed, workdir)
    if method == "routingblocks" and view == "evrptw":
        return _routingblocks(task, seed)
    raise ValueError(f"Initial construction is not defined for {method}/{view}")
