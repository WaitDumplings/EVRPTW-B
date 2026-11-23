"""Exercise method/ID selection and dispatch without starting any solver."""
from collections import Counter
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from runtime import COMBINATIONS
from scripts import report_initial, run_formal as formal


class FormalFilters(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "data"
        self.root.mkdir()
        self.out = Path(tmp.name) / "out"
        self.records = []
        for split, size, ids in [("test1", 100, ["b", "a"]), ("test1", 500, ["d", "c"]),
                                 ("test1", 1000, ["e"]), ("test2", 100, ["g", "f"]),
                                 ("test2", 500, ["h"]), ("xs", 2000, ["i"])]:
            for ident in ids:
                relative = Path("instances", "chicago", split, f"Cus{size}", f"{ident}.parquet")
                file = self.root / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b"")
                self.records.append(dict(city="chicago", split=split, n=size, instance_id=ident,
                                         path=str(relative), bytes=0, sha256=formal.file_hash(file)))
        pq.write_table(pa.Table.from_pylist(self.records), self.root / "index.parquet")

    def config(self, **overrides):
        options = dict(data_root=self.root, cities=None, splits=formal.SPLITS, test2_range=[],
                       seed=20261103, gurobi=False, methods=None, instance_ids=None)
        options.update(overrides)
        return formal.make_manifest(SimpleNamespace(**options))

    def plan(self, *extra):
        stream = io.StringIO()
        with redirect_stdout(stream), patch.object(formal, "reader_environment", side_effect=AssertionError):
            self.assertEqual(formal.main(["--data-root", str(self.root), "--out", str(self.out),
                                          "--plan-only", *extra]), 0)
        self.assertFalse(self.out.exists())
        return stream.getvalue().splitlines()

    def test_legacy_manifest_and_default_pair_order_unchanged(self):
        for enabled in (False, True):
            cfg = self.config(gurobi=enabled)
            self.assertEqual(set(cfg), {"format_version", "index_sha256", "seconds", "threads",
                                        "seed", "gurobi", "inputs"})
            self.assertEqual(len(cfg["inputs"]), len(self.records))
            for row in cfg["inputs"]:
                expected = [(m, v) for m, v in COMBINATIONS
                            if m != "gurobi" or (enabled and row["n"] == 100)]
                self.assertEqual(formal.planned_pairs(row, enabled), expected)

    def test_gurobi_only_implies_enable_and_drops_inapplicable_sizes(self):
        lines = self.plan("--splits", "test1", "test2", "--methods", "gurobi")
        self.assertEqual(lines[0], "# instances=4 tasks=12 core_hours=6")
        tasks = [line.split("\t") for line in lines if "\t" in line][1:]
        self.assertEqual({t[2] for t in tasks}, {"100"})
        self.assertEqual({t[4] for t in tasks}, {"gurobi"})
        self.assertEqual([t[3] for t in tasks][::3], ["a", "b", "f", "g"])
        self.assertEqual({t[5] for t in tasks}, {"cvrp", "vrptw", "evrptw"})

    def test_cus500_requires_explicit_ids_even_with_gurobi_flag(self):
        row = next(r for r in self.records if r["instance_id"] == "c")
        self.assertEqual(formal.planned_pairs(row, True, ["gurobi"]), [])
        lines = self.plan("--methods", "gurobi", "--splits", "test1", "--instance-ids", "d", "c")
        self.assertEqual(lines[0], "# instances=2 tasks=6 core_hours=3")
        cfg = self.config(methods=["gurobi"], instance_ids=["d", "c"], splits=["test1"])
        self.assertEqual(cfg["instance_ids"], ["c", "d"])
        self.assertTrue(cfg["gurobi"])
        self.assertEqual([r["instance_id"] for r in cfg["inputs"]], ["c", "d"])
        cfg = self.config(gurobi=True, instance_ids=["c"])
        self.assertEqual(len(formal.planned_pairs(row, cfg["gurobi"], None, cfg["instance_ids"])), 14)

    def test_explicit_ids_cannot_silently_disappear_or_expand_scope(self):
        for ids in (["missing"], ["c", "c"], []):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.config(methods=["gurobi"], instance_ids=ids)
        for ids in (["e"], ["a", "e"], ["i"]):
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, "no tasks"):
                self.config(methods=["gurobi"], instance_ids=ids)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.config(methods=["gurobi"], splits=["test2"], instance_ids=["a"])
        with self.assertRaisesRegex(ValueError, "outside"):
            self.config(methods=["gurobi"], test2_range=[[100, 2, 2]], instance_ids=["f"])

    def test_filter_order_is_canonical_and_resume_rejects_changed_scope(self):
        cfg = self.config(methods=["gurobi", "ortools", "gurobi"], instance_ids=["c", "a"])
        equivalent = self.config(methods=["ortools", "gurobi"], instance_ids=["a", "c"])
        self.assertEqual(cfg, equivalent)
        with formal.output_lock(self.out, cfg):
            pass
        with formal.output_lock(self.out, equivalent):
            pass
        for changed in (self.config(methods=["ortools"], instance_ids=["c", "a"]),
                        self.config(methods=["ortools", "gurobi"], instance_ids=["a"])):
            with self.assertRaisesRegex(ValueError, "manifest differs"):
                with formal.output_lock(self.out, changed):
                    pass

    def test_dispatch_only_three_gurobi_tasks_and_no_initial_collector(self):
        for ident in ("a", "c"):
            cfg = self.config(methods=["gurobi"], instance_ids=[ident])
            row, = cfg["inputs"]
            commands = []
            args = SimpleNamespace(out=self.out, data_root=self.root, retry_errors=True,
                                   processes=SimpleNamespace(run=lambda command, **kw:
                                                             commands.append((command, kw)) or
                                                             SimpleNamespace(returncode=0)))
            histories = []
            for _ in range(3):
                histories.extend([(None, []), ((self.out / "fake-attempt", "no_feasible_within_budget"), [])])
            with patch.object(formal, "task_history", side_effect=histories):
                result = formal.run_instance(row, 17, args, cfg, {"example": "env"}, (8, 9))
            self.assertEqual(result, Counter(no_feasible_within_budget=3))
            for (command, kwargs), view in zip(commands, ("cvrp", "vrptw", "evrptw")):
                self.assertEqual(command[0:3], ["taskset", "-c", "17"])
                self.assertEqual(command[4:], [str(formal.ROOT / "bench.py"), "run", "--input",
                                 str(self.root / row["path"]), "--solver", "gurobi", "--view", view,
                                 "--cpu", "17", "--threads", "1", "--seconds", "1800", "--seed", "20261103",
                                 "--out", str(self.out), "--expect-sha256", row["sha256"], "--expect-id", ident])
                self.assertEqual(kwargs, dict(cwd=formal.ROOT, env={"example": "env"}, pass_fds=(8, 9)))
            self.assertEqual(len(commands), 3)

    def test_initial_report_uses_the_manifest_filter(self):
        for extra in ({}, dict(methods=["gurobi"], instance_ids=["c"]), dict(methods=["ortools"])):
            cfg = self.config(**extra)
            with patch.object(report_initial, "completed_attempt", return_value=None) as completed:
                rows = list(report_initial.rows(self.out, cfg))
            if cfg.get("methods") == ["gurobi"]:
                self.assertEqual(rows, [])
                completed.assert_not_called()
            else:
                expected = {"ortools"} if cfg.get("methods") else {"ortools", "vns-ts", "routingblocks"}
                self.assertEqual({row["method"] for row in rows}, expected)


if __name__ == "__main__":
    unittest.main()
