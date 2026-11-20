"""Pinned PyVRP ILS and HGS frontends using their respective upstream searches."""

from importlib import import_module
from importlib.metadata import version
from unittest.mock import patch

from .classical import _integers, _scales


def _model(task, modern):
    from pyvrp import Model

    a = _integers(task)
    rows = task.nodes.to_pylist()
    model = Model()
    locations = []
    for i, row in enumerate(rows):
        # 0.9 takes integer XY. Coordinates are only search geometry; all arcs
        # are inserted below from the directed source matrices.
        xy = (round(row["lon"] * 100000), round(row["lat"] * 100000))
        if modern:
            location = model.add_location(*xy)
            args = (location,)
        else:
            args = xy
        if i == 0:
            node = model.add_depot(*args)
        else:
            timing = {}
            if "duration" in a:
                timing = dict(service_duration=int(a["service"][i]),
                              tw_early=int(a["early"][i]), tw_late=int(a["late"][i]))
            node = model.add_client(*args, delivery=int(a["demand"][i]), **timing)
        locations.append(location if modern else node)
    shift = dict(zip(("tw_early", "tw_late"), a.get("shift", ())))
    model.add_vehicle_type(num_available=len(rows) - 1, capacity=a["capacity"],
                           fixed_cost=0, unit_distance_cost=1, unit_duration_cost=0, **shift)
    for i, frm in enumerate(locations):
        for j, to in enumerate(locations):
            duration = int(a["duration"][i, j]) if "duration" in a else 0
            model.add_edge(frm, to, distance=int(a["distance"][i, j]), duration=duration)
    return model.data()


def solve_pyvrp(method, task, ctx):
    import pyvrp

    modern = method == "pyvrp-ils"
    expected = "0.14.0" if modern else "0.9.0"
    if version("pyvrp") != expected:
        raise RuntimeError(f"{method} requires its PyVRP {expected} environment")
    ctx.info(version=expected, algorithm="ILS" if modern else "HGS", **_scales(task))
    data = _model(task, modern)

    def submit(sol):
        if sol.is_feasible():
            if modern:
                # v0.14 uses per-activity-type indices; clients keep their
                # explicit model location. Scheduled routes include depots.
                routes = [[0, *(data.client(a.idx).location for a in r if a.is_client()), 0]
                          for r in sol.routes()]
            else:
                # v0.9 visits already refer to model locations, including
                # the depot in the numbering but excluding it from visits().
                routes = [[0, *r.visits(), 0] for r in sol.routes()]
            ctx.emit(routes, objective_mm=sol.distance())

    def stop(best_cost):
        return ctx.remaining() <= 0

    if modern:
        class Recorder(pyvrp.IteratedLocalSearchCallbacks):
            def on_start(self, ils):
                submit(ils.initial_solution)

            def on_best(self, best):
                submit(best)

        params = pyvrp.SolveParams(ils=pyvrp.IteratedLocalSearchParams(callbacks=Recorder()))
        pyvrp.solve(data, stop=stop, seed=ctx.seed, collect_stats=False, params=params)
    else:
        # The old GA has no public callbacks. Its own constructor, education,
        # and repair routines assign _best at the instant a new incumbent is
        # accepted. Observe those assignments without changing search logic.
        driver = import_module("pyvrp.solve")

        class Recorder(driver.GeneticAlgorithm):
            def __setattr__(self, name, value):
                super().__setattr__(name, value)
                if name == "_best":
                    submit(value)

        with patch.object(driver, "GeneticAlgorithm", Recorder):
            driver.solve(data, stop=stop, seed=ctx.seed, collect_stats=False)
