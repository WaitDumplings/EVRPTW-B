"""Validate the r2 plan/dispatch contract on a real public index; never run a solver."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import bench
from scripts import run_formal as formal

R2 = "bee9d58230f0cfa51d1cadb98e96e2b28ceaab94"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def plan(checkout, data, flags, destination):
    result = subprocess.run([sys.executable, str(checkout / "scripts/run_formal.py"),
                             "--data-root", str(data), "--plan-only", *flags],
                            cwd=checkout, text=True, capture_output=True, check=True)
    if result.stderr:
        raise AssertionError(result.stderr)
    destination.write_text(result.stdout)
    return result.stdout


def capture_dispatch(module, row, cfg, data, out, cpu):
    commands, seen = [], set()

    def history(unused_out, unused_row, method, view, unused_cfg):
        if method != "gurobi":
            return (out / "unused", "setup_error"), []  # Existing non-Gurobi tasks are never dispatched.
        key = (method, view)
        if key in seen:
            return (out / "unused", "no_feasible_within_budget"), []
        seen.add(key)
        return None, []

    def invoke(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    args = SimpleNamespace(data_root=data, out=out, retry_errors=False,
                           processes=SimpleNamespace(run=invoke))
    with patch.object(module, "task_history", side_effect=history):
        module.run_instance(row, cpu, args, cfg, {}, (88, 99))
    return commands


class CommandCaptured(Exception):
    pass


def worker_command(dispatch, row, cpu):
    """Capture the unchanged bench.execute worker argv before any solver process or file write."""
    argv, _ = dispatch
    options = dict(zip(argv[6::2], argv[7::2]))
    captured = []

    def launch(command, unused_out, unused_seconds):
        captured.append(command)
        raise CommandCaptured

    args = SimpleNamespace(out=Path(options["--out"]), seconds=int(options["--seconds"]),
                           seed=int(options["--seed"]), expect_sha256=options["--expect-sha256"],
                           expect_id=options["--expect-id"])
    with (patch.object(bench, "load_view", return_value=SimpleNamespace(info={"instance_id": row["instance_id"]})),
          patch.object(bench, "file_hash", return_value=row["sha256"]),
          patch.object(bench, "attempt_directory", side_effect=lambda path: path / "attempt-001"),
          patch.object(bench, "cpu_topology", return_value={cpu: (0, 0)}),
          patch.object(bench, "launch", side_effect=launch)):
        try:
            bench.execute(Path(options["--input"]), options["--solver"], options["--view"], args, cpu)
        except CommandCaptured:
            pass
    assert len(captured) == 1
    return captured[0]


def verify(data, delivery):
    delivery.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="evrptwb-r2-") as tmp:
        baseline = Path(tmp)
        archive = git("archive", R2)
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(baseline, filter="data")
        summaries = {}
        for label, flags, expected in [("default", [], (3500, 38500, 19250)),
                                       ("with-gurobi", ["--gurobi"], (3500, 41500, 20750))]:
            old = plan(baseline, data, flags, delivery / f"r2-{label}.tsv")
            new = plan(ROOT, data, flags, delivery / f"r3-{label}.tsv")
            assert old.splitlines() == new.splitlines(), label
            assert old.splitlines()[0] == "# instances=%d tasks=%d core_hours=%g" % expected
            summaries[label] = dict(instances=expected[0], tasks=expected[1], core_hours=expected[2],
                                    identical_lines=True, sha256=hashlib.sha256(new.encode()).hexdigest())
        only = plan(ROOT, data, ["--splits", "test1", "test2", "--methods", "gurobi"],
                    delivery / "gurobi-cus100.tsv")
        assert only.splitlines()[0] == "# instances=1000 tasks=3000 core_hours=1500"
        r2_tasks = [line for line in old.splitlines() if "\tgurobi\t" in line]
        new_tasks = [line for line in only.splitlines() if "\tgurobi\t" in line]
        assert new_tasks == r2_tasks and len(new_tasks) == 3000
        rows = formal.select_inputs(data, None, ["test1", "test2"], {})
        probe = sorted(r["instance_id"] for r in rows if r["split"] == "test1" and r["n"] == 500)[:2]
        assert len(probe) == 2
        probe_plan = plan(ROOT, data, ["--splits", "test1", "--methods", "gurobi", "--instance-ids", *probe],
                          delivery / "gurobi-cus500-probe.tsv")
        assert probe_plan.splitlines()[0] == "# instances=2 tasks=6 core_hours=3"
        assert {line.split("\t")[3] for line in probe_plan.splitlines() if "\tgurobi\t" in line} == set(probe)

        spec = importlib.util.spec_from_file_location("formal_r2", baseline / "scripts/run_formal.py")
        legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(legacy)
        # Use the same placement: compare solver arguments, not the temporary baseline checkout's path.
        legacy.ROOT = ROOT
        cpu = min(os.sched_getaffinity(0))
        cfg = dict(seconds=1800, seed=20261103, gurobi=True)
        filtered = dict(cfg, methods=["gurobi"])
        checked = 0
        with (delivery / "worker-parameter-equivalence.jsonl").open("w") as log:
            for row in rows:
                if row["n"] != 100:
                    continue
                original = capture_dispatch(legacy, row, cfg, data, delivery / "unused-output", cpu)
                revised = capture_dispatch(formal, row, filtered, data, delivery / "unused-output", cpu)
                assert original == revised and len(original) == 3
                for before, after in zip(original, revised):
                    worker_before, worker_after = worker_command(before, row, cpu), worker_command(after, row, cpu)
                    assert worker_before == worker_after
                    argv = list(after[0])
                    argv[3] = "$PY"
                    worker_after = list(worker_after)
                    worker_after[0] = "$EVB_PYTHON_GUROBI"
                    log.write(json.dumps(dict(instance_id=row["instance_id"], dispatch=argv,
                                              worker=worker_after, identical=True)) + "\n")
                    checked += 1
        assert checked == 3000
        summaries["gurobi_cus100"] = dict(instances=1000, tasks=3000, core_hours=1500,
                                           identical_task_order=True, identical_dispatch_and_worker_argv=checked)
        summaries["cus500_probe"] = dict(instance_ids=probe, instances=2, tasks=6, core_hours=3)

    protected = []
    for name in git("ls-tree", "-r", "--name-only", R2).decode().splitlines():
        if name.startswith("scripts/") or name == "docs/formal-run.zh-CN.md":
            continue
        before = hashlib.sha256(git("show", f"{R2}:{name}")).hexdigest()
        after = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        assert before == after, name
        protected.append(dict(path=name, r2_sha256=before, current_sha256=after))
    (delivery / "protected-sha256.json").write_text(json.dumps(protected, indent=2) + "\n")
    summaries["protected_files"] = dict(checked=len(protected), identical=True)
    (delivery / "verification.json").write_text(json.dumps(summaries, indent=2) + "\n")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--delivery", type=Path, required=True)
    args = parser.parse_args()
    verify(args.data_root.resolve(), args.delivery.resolve())
