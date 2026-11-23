"""Exercise planning, core ownership and restart without invoking a solver."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from bench import checkpoint_times
from initial_record import IDENTITY, INITIAL_PAIRS, SCHEMA
from runtime import COMBINATIONS, write_json
from scripts import run_formal as formal


class FormalScheduler(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "data"
        self.root.mkdir()
        self.records = []
        for split, size, ids in [("test2", 100, ["c", "a", "b"]), ("test2", 500, ["g", "e", "f"]),
                                 ("test1", 100, ["z", "y"]), ("xs", 2000, ["x"]), ("train", 100, ["t"])]:
            city = "jacksonville" if split == "test2" else "chicago"
            for ident in ids:
                path = Path("instances", city, split, f"Cus{size}", f"{ident}.parquet")
                (self.root / path).parent.mkdir(parents=True, exist_ok=True)
                (self.root / path).write_bytes(b"")
                self.records.append(dict(city=city, split=split, n=size, instance_id=ident, bytes=0,
                                         sha256=formal.file_hash(self.root / path), path=str(path)))
        pq.write_table(pa.Table.from_pylist(self.records), self.root / "index.parquet")
        self.out = self.base / "out"
        self.args = SimpleNamespace(out=self.out, data_root=self.root, retry_errors=False)
        self.args.processes = SimpleNamespace(run=None, check_cancelled=lambda: None)
        self.cfg = dict(seed=20261103, seconds=1800, gurobi=False)
        self.row = next(r for r in self.records if r["instance_id"] == "y")

    def test_plan_only_orders_and_reports_the_selected_panel_without_writes(self):
        args = ["--data-root", str(self.root), "--out", str(self.out), "--plan-only",
                "--splits", "test2", "xs", "test1", "--test2-range", "2", "3",
                "--test2-range", "500", "1", "2", "--gurobi"]
        stream = io.StringIO()
        with redirect_stdout(stream), patch.object(formal, "reader_environment", side_effect=AssertionError):
            self.assertEqual(formal.main(args), 0)
        lines = stream.getvalue().splitlines()
        self.assertIn("instances=7 tasks=89 core_hours=44.5", lines[0])
        tasks = [line.split("\t") for line in lines if "\t" in line][1:]
        self.assertEqual(list(dict.fromkeys(t[3] for t in tasks)), ["y", "z", "x", "b", "c", "e", "f"])
        self.assertEqual(len(tasks), len({tuple(t) for t in tasks}))
        self.assertTrue(all(t[2] == "100" for t in tasks if t[4] == "gurobi"))
        self.assertFalse(self.out.exists())
        self.assertEqual(len(formal.planned_pairs(self.row, False)), 11)
        only_500 = formal.select_inputs(self.root, ["jacksonville"], ["test2"], {500:(2, 2)})
        self.assertEqual([r["instance_id"] for r in only_500], ["f"])
        with self.assertRaisesRegex(ValueError, "requested"):
            formal.select_inputs(self.root, None, ["test2"], {None:(1, 4)})
        with self.assertRaises(ValueError):
            formal.parse_ranges([[100, 0, 2]])

    def test_resume_only_collects_missing_sidecar_and_keeps_recorded_errors(self):
        history = []
        self.out.mkdir()
        self.args.progress = formal.Progress(self.out)

        def mock_run(argv, *, cwd, env, pass_fds):
            self.assertEqual(argv[:3], ["taskset", "-c", "17"])
            self.assertEqual(pass_fds, (8, 9))
            if "--attempt" in argv:
                folder = Path(argv[argv.index("--attempt") + 1])
                result = json.loads((folder / "result.json").read_text())
                write_json(folder / "initial_solution.json",
                           {**{k: result[k] for k in IDENTITY}, "schema": SCHEMA, "status": "no_initial_solution"})
                history.append("initial")
                return SimpleNamespace(returncode=0)
            method, view = (argv[argv.index(flag) + 1] for flag in ("--solver", "--view"))
            k = COMBINATIONS.index((method, view)) + 1
            folder = self.out / self.row["instance_id"] / f"{k:02d}-{method}-{view}"
            target = folder / f"attempt-{len(list(folder.glob('attempt-*'))) + 1:03d}"
            target.mkdir(parents=True)
            times = checkpoint_times(self.cfg["seconds"])
            write_json(target / "checkpoints.json", [dict(at_s=t, best=None) for t in times])
            status = "license_blocked" if method == "hgs" else "no_feasible_within_budget"
            write_json(target / "result.json", dict(sha256=self.row["sha256"], instance_id=self.row["instance_id"],
                       method=method, view=view, seed=self.cfg["seed"], seconds=self.cfg["seconds"], threads=1,
                       checkpoint_times_s=times, status=status))
            history.append("run")
            return SimpleNamespace(returncode=1)

        def dispatch():
            return formal.run_instance(self.row, 17, self.args, self.cfg, {}, (8, 9))

        with patch.object(self.args.processes, "run", side_effect=mock_run):
            self.assertEqual(dispatch(), Counter(no_feasible_within_budget=10, license_blocked=1))
            self.assertEqual(history, ["run"] * 11 + ["initial"] * len(INITIAL_PAIRS))
            history.clear()
            dispatch()
            self.assertEqual(history, [])
            next(self.out.rglob("initial_solution.json")).unlink()
            dispatch()
            self.assertEqual(history, ["initial"])
            history.clear()
            self.args.retry_errors = True
            dispatch()
            self.assertEqual(history, ["run"])
            self.assertEqual(len(list(self.out.rglob("attempt-002/result.json"))), 1)
            progress = self.out / "progress.json"
            failures = json.loads(progress.read_text())["final_failures"]
            self.assertEqual([(r["method"], len(r["attempts"])) for r in failures], [("hgs", 2)])
            stamp = progress.stat().st_mtime_ns
            self.args.progress = formal.Progress(self.out)
            history.clear()
            dispatch()
            self.assertEqual(history, [])
            self.assertEqual(progress.stat().st_mtime_ns, stamp)

    def test_legacy_false_pass_and_interrupted_attempt_count_toward_different_limits(self):
        self.out.mkdir()
        self.args.progress = formal.Progress(self.out)
        self.args.retry_errors = True
        times = checkpoint_times(self.cfg["seconds"])
        common = dict(sha256=self.row["sha256"], instance_id=self.row["instance_id"], view="cvrp",
                      seed=self.cfg["seed"], seconds=self.cfg["seconds"], threads=1, checkpoint_times_s=times)
        folder = self.out / self.row["instance_id"] / "01-ortools-cvrp"
        archived = folder / "attempt-001"
        archived.mkdir(parents=True)
        write_json(archived / "result.json", dict(**common, method="ortools", status="pass", returncode=-9,
                                                  watchdog=False, wall_s=0.2))
        write_json(archived / "checkpoints.json", [dict(at_s=t, best=None) for t in times])
        write_json(archived / "routes.json", {})
        write_json(archived / "replay.json", {})
        raw = (archived / "result.json").read_bytes()
        partial = folder / "attempt-002"
        partial.mkdir()
        (partial / "events.jsonl").write_text("")
        history = []

        def simulate(argv, **kwargs):
            self.assertNotIn("--attempt", argv)
            method = argv[argv.index("--solver") + 1]
            target = (folder / "attempt-003" if method == "ortools" else
                      self.out / self.row["instance_id"] / "03-hgs-cvrp" / "attempt-001")
            target.mkdir(parents=True)
            status = "execution_error" if method == "ortools" else "no_feasible_within_budget"
            write_json(target / "result.json",
                       dict(**common, method=method, status=status, reason="simulated process failure"))
            write_json(target / "checkpoints.json", [dict(at_s=t, best=None) for t in times])
            history.append(method)
            return SimpleNamespace(returncode=int(method == "ortools"))

        with patch.object(formal, "planned_pairs", return_value=[("ortools", "cvrp"), ("hgs", "cvrp")]), \
             patch.object(self.args.processes, "run", side_effect=simulate):
            def dispatch():
                return formal.run_instance(self.row, 17, self.args, self.cfg, {}, (8, 9))

            self.assertEqual(dispatch(), Counter(execution_error=1, no_feasible_within_budget=1))
            self.assertEqual(history, ["ortools", "hgs"])
            progress = self.out / "progress.json"
            failure, = json.loads(progress.read_text())["final_failures"]
            self.assertEqual(failure["instance_id"], self.row["instance_id"])
            self.assertEqual([Path(a["path"]).name for a in failure["attempts"]], ["attempt-001", "attempt-003"])
            self.assertIn("-9", failure["attempts"][0]["reason"])
            self.assertEqual(failure["attempts"][1]["reason"], "simulated process failure")
            self.assertEqual((archived / "result.json").read_bytes(), raw)

            # Recover a cancellation between publishing the second result and updating progress.
            write_json(progress, {"final_failures": []})
            self.args.progress = formal.Progress(self.out)
            dispatch()
            self.assertEqual(history, ["ortools", "hgs"])
            self.assertEqual(json.loads(progress.read_text())["final_failures"], [failure])

    def test_concurrent_final_failures_both_survive_restart(self):
        self.out.mkdir()
        progress = formal.Progress(self.out)
        barrier = threading.Barrier(2)
        rows = [r for r in self.records if r["split"] == "test1"]

        def finish(row):
            folder = self.out / row["instance_id"] / "03-hgs-cvrp"
            failures = []
            for number in (2, 1):
                attempt = folder / f"attempt-{number:03d}"
                attempt.mkdir(parents=True)
                write_json(attempt / "result.json", {"status": "setup_error", "reason": "bad environment"})
                failures.append((attempt, "setup_error"))
            barrier.wait(timeout=5)
            progress.record_final_failure(row, "hgs", "cvrp", failures)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(finish, rows))
        failures = json.loads((self.out / "progress.json").read_text())["final_failures"]
        self.assertEqual([r["instance_id"] for r in failures], sorted(r["instance_id"] for r in rows))
        self.assertEqual(len(formal.Progress(self.out).failures), 2)

    def test_scope_lock_and_independent_physical_core_leases(self):
        reps = {(0, 4): 8, (0, 5): 9}
        locks = self.base / "locks"
        with formal.output_lock(self.out, self.cfg):
            with self.assertRaisesRegex(ValueError, "active"):
                with formal.output_lock(self.out, self.cfg):
                    pass
            with formal.physical_leases(reps, 1, locks) as a, formal.physical_leases(reps, 2, locks) as b:
                self.assertEqual(set(a), {8})
                self.assertEqual(set(b), {9})
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            with formal.output_lock(self.out, dict(self.cfg, gurobi=True)):
                pass

    def test_dispatch_honors_size_and_split_barriers(self):
        rows = formal.select_inputs(self.root, None, formal.SPLITS, {})
        events, active = [], set()
        guard = threading.Lock()
        simultaneous = threading.Barrier(2)

        def job(row, cpu, args, cfg, env, fds):
            with guard:
                self.assertNotIn(cpu, active)
                active.add(cpu)
                events.append(("start", formal.phase(row)))
            if row["split"] == "test1":
                simultaneous.wait(timeout=5)
            with guard:
                active.remove(cpu)
                events.append(("end", formal.phase(row)))
            return Counter(pass_count=1)

        with patch.object(formal, "run_instance", side_effect=job), redirect_stdout(io.StringIO()):
            totals = formal.schedule(self.args, dict(self.cfg, inputs=rows), {}, {8:42,9:43}, 99)
        self.assertEqual(totals["pass_count"], len(rows))
        self.assertEqual([e[1] for e in events], sorted(e[1] for e in events))
