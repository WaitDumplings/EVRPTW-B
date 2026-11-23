"""Vehicle bounds for the two LKH views, exercised without either native solver."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pyarrow as pa

import bench
from adapters.non_learning import lkh
from replay import evaluate


def fixture(travel, demand, capacity, shift, early, late, service, view="vrptw", distance=None):
    size = len(demand)
    nodes = pa.table({
        "node_idx": list(range(size)),
        "type": ["depot"] + ["customer"] * (size - 1),
        "demand_cm3": demand,
        "ready_s": early,
        "due_s": late,
        "service_s": service,
    })
    vehicle = {"operating_window_s": shift, "cargo_capacity_cm3": capacity}
    return SimpleNamespace(
        nodes=nodes,
        matrices={"shortest_distance_m": np.asarray(travel if distance is None else distance, dtype=float),
                  "shortest_distance_time_s": np.asarray(travel, dtype=float)},
        info={"problem_type": view, "capacity_cm3": capacity, "vehicle_json": json.dumps(vehicle)},
    )


def problem(task):
    with tempfile.TemporaryDirectory() as d:
        output = Path(d) / "problem.vrp"
        meta = lkh._problem(task, output)
        return meta, output.read_text()


class TestLKHVehicleBound(unittest.TestCase):
    def test_unit_loads_and_rounded_int32_boundaries(self):
        task = SimpleNamespace(info={"capacity_cm3": 7.9})
        self.assertEqual(lkh._loads(task, {"demand_cm3": [0, 3.00001, 3.0]}), ([0, 4, 3], 7, 1))
        task.info["capacity_cm3"] = lkh.INT_MAX + 0.75
        self.assertEqual(lkh._loads(task, {"demand_cm3": [0, lkh.INT_MAX]}),
                         ([0, lkh.INT_MAX], lkh.INT_MAX, 1))
        with self.assertRaisesRegex(ValueError, "int32"):
            lkh._loads(task, {"demand_cm3": [0, lkh.INT_MAX + 0.125]})
        task.info["capacity_cm3"] = lkh.INT_MAX + 1
        with self.assertRaisesRegex(ValueError, "int32"):
            lkh._loads(task, {"demand_cm3": [0, 1]})
        task.info["capacity_cm3"] = 1.9
        with self.assertRaisesRegex(ValueError, "exceeds capacity"):
            lkh._loads(task, {"demand_cm3": [0, 1.5]})

    def test_capacity_only_bound_and_emitted_integer_demands(self):
        # Rounded demands 4,4,4 cannot share bins of size 7.
        task = fixture(np.zeros((4, 4)), [0, 3.1, 3.1, 3.1], 7.99,
                       [0, 100], [0] * 4, [100] * 4, [0] * 4, "cvrp")
        meta, native = problem(task)
        self.assertEqual((meta["vehicle_bound_basis_vehicles"], meta["vehicle_upper_bound"]), (3, 5))
        self.assertEqual((meta["demand_ticks_per_cm3"], meta["internal_nodes"]), (1, 16))
        self.assertIn("CAPACITY: 7\nVEHICLES: 5\n", native)
        self.assertIn("DEMAND_SECTION\n1 0\n2 4\n3 4\n4 4\n", native)

    def test_quarter_margin_and_determinism(self):
        # Nine 6-second services cannot share a 10-second depot shift.
        n = 9
        zero = np.zeros((n+1, n+1))
        task = fixture(zero, [0]+[1]*n, 20, [5, 15], [0]*(n+1), [100]*(n+1), [0]+[6]*n)
        meta, native = problem(task)
        self.assertEqual(meta["vehicle_bound_basis"], "vrptw_feasible_greedy_insertion")
        self.assertEqual((meta["vehicle_bound_basis_vehicles"], meta["vehicle_upper_bound"]), (9, 12))
        self.assertEqual(meta["internal_nodes"], 42)
        self.assertNotIn("TOUR_SECTION", native)
        self.assertEqual(native, problem(task)[1])
        self.assertGreaterEqual(meta["vehicle_bound_construction_s"], 0)

    def test_insert_in_asymmetric_nonmetric_gap(self):
        # Customer 2 misses its deadline when approached from the depot,
        # and can only be served after customer 1 on its existing route.
        matrix = np.array([[0, 1, 30], [3, 0, 2], [3, 40, 0]])
        starts = np.array([0, 1, 5])
        ends = np.array([50, 1, 20])
        service = np.array([0, 1, 2])
        times = (matrix, service, starts, ends)
        first = lkh._greedy_routes(matrix, [0, 1, 1], 5, times)
        self.assertEqual(first, [[0, 1, 2, 0]])
        task = fixture(matrix, [0, 1, 1], 5, [0, 50], starts, ends, service)
        self.assertTrue(evaluate(task, first)["valid"])
        self.assertEqual(problem(task)[0]["vehicle_upper_bound"], 3)

    def test_existing_successor_deadline_and_capacity(self):
        # A zero-distance insertion before 1 would delay it beyond its
        # deadline. Only inserting 2 after 1 is feasible on that route.
        weights = np.array([[0, 9, 0], [1, 0, 7], [2, 0, 0]])
        travel = np.array([[0, 1, 2], [1, 0, 2], [1, 4, 0]])
        early = np.array([0, 0, 0])
        late = np.array([15, 2, 8])
        service = np.array([0, 1, 2])
        times = (travel, service, early, late)
        self.assertEqual(lkh._greedy_routes(weights, [0, 3, 3], 6, times), [[0, 1, 2, 0]])
        self.assertEqual(lkh._greedy_routes(weights, [0, 3, 3], 5, times), [[0, 1, 0], [0, 2, 0]])

    def test_no_witness_reports_customer(self):
        travel = [[0, 10, 1], [1, 0, 1], [1, 1, 0]]
        task = fixture(travel, [0, 1, 1], 5, [0, 10], [0, 0, 0], [10, 3, 10], [0, 0, 0])
        with self.assertRaisesRegex(ValueError, "no feasible insertion or singleton.*customer 1"):
            problem(task)

    def test_bound_reached_describes_the_best_replayed_solution(self):
        travel = [[0, 10, 10], [10, 0, 1], [10, 1, 0]]
        task = fixture(travel, [0, 1, 1], 10, [0, 100], [0]*3, [100]*3, [0]*3, "cvrp")
        task.info["instance_id"] = "bounds"
        split = [[0, 1, 0], [0, 2, 0]]
        joined = [[0, 1, 2, 0]]
        for offered, expected, vehicles in [([split], True, 2), ([split, joined], False, 1),
                                            ([], None, None)]:
            with self.subTest(offered=offered), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "input"
                source.touch()
                args = SimpleNamespace(out=root, seconds=10, seed=1, expect_sha256=None, expect_id=None)

                def launch(command, out, seconds):
                    events = [{"kind": "info", "t": 0.0, "data": {"vehicle_upper_bound": 2}}]
                    events += [{"kind": "candidate", "t": i + 1.0, "data": {"routes": routes, "meta": {}}}
                               for i, routes in enumerate(offered)]
                    events.append({"kind": "finish", "t": 9.0, "data": {"status": "returned"}})
                    (out / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
                    return {"watchdog": False, "observed_worker_exit_s": 9.0, "returncode": 0}

                with patch.object(bench, "load_view", return_value=task), patch.object(bench, "launch", launch):
                    dest, result = bench.execute(source, "lkh", "cvrp", args, min(bench.cpu_topology()))
                self.assertEqual(result["status"], "pass" if offered else "no_feasible_within_budget")
                self.assertEqual(result.get("vehicles"), vehicles)
                self.assertIs(json.loads((dest / "result.json").read_text())["vehicle_bound_reached"], expected)


if __name__ == "__main__":
    unittest.main()
