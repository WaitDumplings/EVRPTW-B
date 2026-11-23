"""Protocol regressions using tiny directed routes and recorded optimizer bounds."""
from contextlib import contextmanager
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pyarrow as pa

import worker
from bench import checkpoint_times, inspect_events
from scripts.report_test1 import FIELDS, report


class CheckpointProtocol(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.task = SimpleNamespace(
            info={"problem_type": "cvrp", "vehicle_json": '{"cargo_capacity_cm3": 2}'},
            nodes=pa.table({"node_idx": [0, 1, 2], "type": ["depot", "customer", "customer"],
                            "demand_cm3": [0, 1, 1]}),
            matrices={"shortest_distance_m": np.array([[0, 3, 1], [1, 0, 1], [2, 1, 0]])},
        )

    def inspect(self, events, seconds, method):
        path = self.base / "events.jsonl"
        with path.open("w") as stream:
            for stamp, kind, data in events:
                stream.write(json.dumps(dict(t=stamp, kind=kind, data=data)) + "\n")
            stream.write('{"kind":"candidate"')  # incomplete line from a killed worker
        return inspect_events(path, self.task, seconds, method=method)

    def test_inclusive_cutoff_and_independent_route_replay(self):
        split, joined, reverse = [[0, 1, 0], [0, 2, 0]], [[0, 1, 2, 0]], [[0, 2, 1, 0]]
        before, after = 60.0, 60.000001
        history = [(t, "candidate", dict(routes=route, meta={"claimed_m": -1}))
                   for t, route in [(5, [[0, 1, 0]]), (before, split), (before, joined), (after, reverse)]]
        history.extend([(80, "finish", {"status": "returned"}), (120.01, "candidate", {"routes": []})])
        best, first, checkpoints, _, rejected, n, _ = self.inspect(history, 120, "hgs")
        self.assertEqual((n, len(rejected), first), (4, 1, before))
        self.assertEqual([row["at_s"] for row in checkpoints], [60, 120])
        self.assertEqual([(row["best"]["routes"], row["best"]["distance_m"]) for row in checkpoints],
                         [(joined, 6), (reverse, 3)])
        self.assertEqual(best, checkpoints[-1]["best"])
        self.assertNotIn("upper_bound_m", checkpoints[0])
        self.assertEqual(checkpoint_times(45), [45])

    def test_model_ready_handshake_excludes_build_time(self):
        ready_read, ready_write = os.pipe()
        go_read, go_write = os.pipe()
        self.addCleanup(os.close, ready_read)
        os.set_blocking(ready_read, False)
        with os.fdopen(go_write, "w") as go:
            go.write("10.0\n")
        clock = 4.0

        @contextmanager
        def prepare(task, view, seed):
            nonlocal clock
            with self.assertRaises(BlockingIOError):
                os.read(ready_read, 1)
            clock = 10.0  # Six seconds of assembly before a two-second search.
            yield task

        def optimize(task, ctx, prepared):
            self.assertIs(task, prepared)
            self.assertEqual(json.loads(os.read(ready_read, 1024)), {"model_build_s": 6.0})
            self.assertEqual((ctx.elapsed(), ctx.deadline), (0.0, 12.0))

        argv = ["worker.py", "--method", "gurobi", "--view", "cvrp", "--seed", "1",
                "--input", "fixture", "--cpu", "0", "--seconds", "2", "--out", str(self.base),
                "--go-fd", str(go_read), "--ready-fd", str(ready_write)]
        adapter = SimpleNamespace(prepare=prepare, solve_prepared=optimize)
        with (patch.object(sys, "argv", argv), patch.object(os, "sched_setaffinity"),
              patch.object(worker, "load_view", return_value=self.task),
              patch.object(worker.time, "monotonic", side_effect=lambda: clock),
              patch.object(worker.importlib, "import_module", return_value=adapter)):
            worker.main()
        event = json.loads((self.base / "events.jsonl").read_text())
        self.assertEqual((event["kind"], event["data"]["status"], event["t"]), ("finish", "returned", 0))

    def test_model_bound_corrections_without_incumbent_route(self):
        history = [
            (5, "info", dict(bound_scope="full_model", lower_bound_m=3, upper_bound_m=7)),
            (60, "info", dict(lower_bound_m=2)),
            (61, "info", dict(upper_bound_m=None)),
            (601, "candidate", dict(routes=[[0, 2, 1, 0]], meta={"candidate_objective_m": 3})),
            (1499, "info", dict(lower_bound_m=None, upper_bound_m=8)),
            (1801, "info", dict(lower_bound_m=3, upper_bound_m=3)),
        ]
        _, _, rows, *_ = self.inspect(history, 1800, "gurobi")
        self.assertEqual([r["at_s"] for r in rows], [60, 300, 600, 900, 1200, 1500, 1800])
        self.assertEqual([(r["lower_bound_m"], r["upper_bound_m"]) for r in rows],
                         [(2, 7), (2, None), (2, None), (2, None), (2, None), (None, 8), (None, 8)])
        self.assertIsNone(rows[0]["best"])
        self.assertEqual(rows[3]["best"]["distance_m"], 3)
        self.assertIsNone(rows[3]["upper_bound_m"])
        self.assertEqual(rows[-1]["best"], rows[3]["best"])

    def test_unknown_bound_stays_empty_in_csv_and_legacy_keeps_its_own_schedule(self):
        folder = self.base / "i" / "10-gurobi-cvrp" / "attempt-001"
        folder.mkdir(parents=True)
        result = dict(input="instances/chicago/test1/Cus100/i.parquet", seconds=1800, method="gurobi",
                      instance_id="i", threads=1, status="pass", view="cvrp", model_build_s=2,
                      adapter={"lower_bound_m": 3, "upper_bound_m": 3})
        (folder / "result.json").write_text(json.dumps(result))
        best = dict(routes=[[0, 2, 1, 0]], elapsed_s=10, distance_m=3, vehicles=1,
                    meta={"upper_bound_m": 6})
        rows = [dict(at_s=t, best=best, upper_bound_m=None) for t in (60, 300, 900, 1800)]
        (folder / "checkpoints.json").write_text(json.dumps(rows))
        stream = io.StringIO()
        writer = csv.DictWriter(stream, FIELDS)
        writer.writeheader()
        report(self.base, writer)
        output = list(csv.DictReader(io.StringIO(stream.getvalue())))
        self.assertEqual([r["checkpoint_s"] for r in output], ["60", "300", "900", "1800"])
        self.assertTrue(all(not r["upper_bound_m"] and not r["attempt_issues"] for r in output))
        self.assertEqual([r["lower_bound_m"] for r in output], ["", "", "", "3"])


if __name__ == "__main__":
    unittest.main()
