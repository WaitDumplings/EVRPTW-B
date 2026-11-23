import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pyarrow as pa

from initial_record import measure_routes, matches_initial
from runtime import file_hash
from scripts.collect_initial import collect, handoff, main
from scripts.report_initial import summarize


class InitialRecords(unittest.TestCase):
    def setUp(self):
        vehicle = dict(cargo_capacity_cm3=10, operating_window_s=[0, 10], battery_capacity_kwh=3,
                       initial_soc=1, reserve_soc=0.5, specific_energy_kwh_per_km=1)
        self.task = SimpleNamespace(
            info=dict(problem_type="evrptw", instance_id="abc", city="city", split="val",
                      customer_count=2, vehicle_json=json.dumps(vehicle)),
            nodes=pa.table(dict(node_idx=[0, 1, 2, 3], type=["depot","customer","customer","charging"],
                                demand_cm3=[0,6,2,0], ready_s=[0,0,0,0], due_s=[100,1,100,100],
                                service_s=[0,1,0,0], charge_kw=[None,None,None,3600])),
            matrices=dict(shortest_distance_m=np.array([[0,2000,20,1],[500,0,20,1000],
                          [20,20,0,20],[10,1000,20,0]], dtype=float),
                          shortest_distance_time_s=np.array([[0,2,1,1],[3,0,1,2],[1,1,0,1],[1,3,1,0]])))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.attempt = Path(self.tmp.name)
        source = self.attempt / "source"
        source.write_text("sample")
        self.result = dict(sha256=file_hash(source), input=str(source), instance_id="abc", view="evrptw",
                           method="vns-ts", seed=1, seconds=1800, start_monotonic=1., candidates=1)
        self.path = self.attempt / "initial_solution.json"

    def test_all_violations_are_measured_after_first_rejection(self):
        rec = measure_routes(self.task, [[0,1,3,1,0]])
        self.assertEqual((rec["distance_m"], rec["vehicles"], rec["charging_visits"]), (4500,1,1))
        self.assertFalse(rec["feasible"])
        self.assertEqual(rec["violations"], {
            "customer_coverage": {"missing_customers":1,"duplicate_visits":1},
            "capacity": {"routes":1, "total_excess_cm3":2},
            "time_window": {"late_customer_visits":2, "customer_lateness_s":11,
                            "late_returns":1, "shift_overtime_s":5},
            "energy": {"arrivals_below_reserve":2, "sum_arrival_deficits_kwh":2,
                       "max_arrival_deficit_kwh":1.5}})

    def test_no_solution_and_unobserved_run_are_distinct_and_idempotent(self):
        with patch("scripts.collect_initial.load_view", return_value=self.task), \
             patch("adapters.non_learning.initial.construct") as construct:
            absent = dict(self.result, start_monotonic=None, candidates=0)
            collect(self.attempt, absent)
            self.assertEqual(json.loads(self.path.read_text())["status"], "no_initial_solution")
            construct.assert_not_called()
            self.path.unlink()
            construct.return_value=dict(routes=[[0,1,2,0]], construction_s=0.25)
            collect(self.attempt, dict(self.result, candidates=0))
            data=json.loads(self.path.read_text())
            self.assertEqual(data["status"], "unknown")
            self.assertEqual(data["reproduction"]["routes"], [[0,1,2,0]])
            self.assertIsNone(data["distance_m"])
            self.path.unlink()
            collect(self.attempt, self.result)
            before=self.path.stat().st_mtime_ns, self.path.read_bytes()
            collect(self.attempt, self.result)
            self.assertEqual(before,(self.path.stat().st_mtime_ns, self.path.read_bytes()))
            self.assertEqual(construct.call_count, 2)
            self.assertTrue(matches_initial(json.loads(before[1]),self.result))

    def test_normal_timeout_can_precede_the_first_raw_route(self):
        timed = dict(self.result, candidates=0, termination={"status": "returned"})
        self.assertEqual(handoff(dict(timed, adapter={"runs": 0}))[0], "absent")
        self.assertEqual(handoff(dict(timed, adapter={"runs": 1}))[0], "observed")
        rb = dict(timed, method="routingblocks", adapter={"iterations": 1})
        self.assertEqual(handoff(rb)[0], "unconfirmed")
        with patch("scripts.collect_initial.load_view", return_value=self.task), \
             patch("adapters.non_learning.initial.construct",
                   return_value=dict(routes=[[0,1,2,0]], construction_s=0.1)):
            collect(self.attempt, rb)
        record = json.loads(self.path.read_text())
        self.assertEqual(record["status"], "unknown")
        self.assertIn("distance_m", record["reproduction"])

    def test_returned_without_assignment_preserves_attempted_construction_duration(self):
        r = dict(self.result, method="ortools", view="cvrp", candidates=0, termination={"status":"returned"})
        with patch("scripts.collect_initial.load_view", return_value=self.task), \
             patch("adapters.non_learning.initial.construct", return_value=dict(routes=None, construction_s=0.125)):
            collect(self.attempt, r)
        archived = json.loads(self.path.read_text())
        self.assertEqual((archived["status"], archived["construction_s"], archived["routes"]),
                         ("no_initial_solution",0.125,None))

    def test_missing_method_interpreter_still_archives_proven_absence(self):
        absent = dict(self.result, method="ortools", view="cvrp", candidates=0,
                      start_monotonic=None, status="setup_error")
        (self.attempt / "result.json").write_text(json.dumps(absent))
        with patch("sys.argv", ["collect_initial.py", "--attempt", str(self.attempt)]), \
             patch("scripts.collect_initial.load_view", return_value=self.task), \
             patch("scripts.collect_initial.interpreter", side_effect=AssertionError):
            main()
        self.assertEqual(json.loads(self.path.read_text())["status"], "no_initial_solution")

    def test_summary_keeps_all_tasks_and_signed_improvements(self):
        rows=[(dict(status="recorded", feasible=False, distance_m=50.),60.),
              (dict(status="recorded", feasible=True, distance_m=100.),80.),
              (dict(status="no_initial_solution"),None),(dict(status="unknown"),None),(None,None)]
        r=summarize(rows)
        self.assertEqual((r["tasks"],r["recorded"],r["pending"],r["feasible_rate"]),(5,2,1,0.2))
        self.assertEqual((r["mean_initial_m"],r["paired"],r["mean_improvement_m"],
                          r["mean_improvement_pct"]),(75.,2,5.,0.))
