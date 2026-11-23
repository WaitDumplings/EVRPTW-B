"""Exercise status precedence without changing solver timing or input."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bench
from scripts.audit_results import assessment_issues
from scripts.report_test1 import select_attempt


class WorkerExitTest(unittest.TestCase):
    def test_failure_keeps_only_diagnostic_files(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input"
            source.touch()
            args = SimpleNamespace(out=root, seconds=120, seed=1, expect_sha256=None, expect_id=None)
            task = SimpleNamespace(info={"instance_id": "case"})
            journal = b'{"kind":"candidate","t":0.1,"data":{"routes":[[0,1,0]]}}\n'

            def interrupted(command, out, seconds):
                (out / "events.jsonl").write_bytes(journal)
                (out / "solver.log").touch()
                return {"returncode": -9, "watchdog": False, "observed_worker_exit_s": 0.5}

            with (patch.object(bench, "load_view", return_value=task),
                  patch.object(bench, "cpu_topology", return_value={0: (0, 0)}),
                  patch.object(bench, "launch", side_effect=interrupted)):
                out, result = bench.execute(source, "ortools", "cvrp", args, 0)
            self.assertEqual(result["status"], "execution_error")
            self.assertEqual(result["wall_s"], 0.5)
            self.assertEqual(result["returncode"], -9)
            self.assertEqual((out / "events.jsonl").read_bytes(), journal)
            self.assertEqual(json.loads((out / "result.json").read_text()), result)
            selected, issues = select_attempt(root, [(1, out)], "case", "ortools", "cvrp", {}, None)
            self.assertEqual(selected[2]["status"], "execution_error")
            self.assertEqual(selected[3], [])
            self.assertEqual([issue["status"] for issue in issues], ["execution_error"])
            self.assertEqual({f.name for f in out.iterdir()}, {"events.jsonl", "solver.log", "result.json"})

    def test_early_zero_exit_without_finish_keeps_candidate_only_in_journal(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input"
            source.touch()
            args = SimpleNamespace(out=root, seconds=120, seed=1, expect_sha256=None, expect_id=None)
            task = SimpleNamespace(info={"instance_id": "case"})

            def lost_finish(command, out, seconds):
                candidate = dict(kind="candidate", t=.1, data={"routes": [[0, 1, 0]]})
                (out / "events.jsonl").write_text(json.dumps(candidate) + "\n")
                return {"returncode": 0, "watchdog": False, "observed_worker_exit_s": 0.5}

            with (patch.object(bench, "load_view", return_value=task),
                  patch.object(bench, "cpu_topology", return_value={0: (0, 0)}),
                  patch.object(bench, "evaluate", return_value={"distance_m": 5}),
                  patch.object(bench, "launch", side_effect=lost_finish)):
                out, result = bench.execute(source, "ortools", "cvrp", args, 0)
            self.assertEqual(result["status"], "execution_error")
            self.assertIn("without a normal return", result["reason"])
            self.assertEqual(result["candidates"], 1)
            self.assertEqual({f.name for f in out.iterdir()}, {"events.jsonl", "result.json"})

    def test_real_worker_exit_attribution(self):
        handshake = (
            "import os, sys, time; "
            "ready = int(sys.argv[sys.argv.index('--ready-fd')+1]); "
            "go = int(sys.argv[sys.argv.index('--go-fd')+1]); "
            "os.write(ready, b'{}\\n'); os.close(ready); os.read(go, 128); "
        )
        with TemporaryDirectory() as temp:
            out = Path(temp)
            for action, code, watchdog in [("pass", 0, False), ("sys.exit(7)", 7, False),
                                           ("time.sleep(30)", -9, True)]:
                with self.subTest(action=action):
                    result = bench.launch([sys.executable, "-c", handshake + action], out, 0.15)
                    self.assertEqual((result["returncode"], result["watchdog"]), (code, watchdog))
            before_ready = bench.launch([sys.executable, "-c", "raise SystemExit(8)"], out, 1)
            self.assertEqual((before_ready["status"], before_ready["returncode"]), ("execution_error", 8))

    def test_audit_uses_observed_clock_and_return_evidence(self):
        valid = dict(status="pass", returncode=0, seconds=120, wall_s=3,
                     observed_worker_exit_s=3.1, termination={"status": "returned"}, watchdog=False)
        self.assertEqual(assessment_issues(valid), [])
        self.assertEqual(assessment_issues(dict(valid, returncode=-9, watchdog=True,
                                                termination=None, observed_worker_exit_s=120.1)), [])
        killed = dict(valid, returncode=-9, termination=None)
        self.assertEqual(len(assessment_issues(killed)), 2)
        self.assertEqual(assessment_issues(dict(killed, status="execution_error")), [])
        shortened = dict(valid, status="no_feasible_within_budget", watchdog=True, wall_s=120, termination=None)
        self.assertEqual(len(assessment_issues(shortened)), 1)
