#!/usr/bin/env python3
"""Summarize Test1 bench.py attempts and export their actual checkpoint observations."""
import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import NamedTemporaryFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import COMBINATIONS


FIELDS = (
    "city", "size", "slot", "instance_id", "sha256", "method", "view", "seed",
    "budget_s", "status", "reason", "wall_s", "first_feasible_s", "checkpoint_s",
    "incumbent_s", "distance_m", "vehicles", "charging_visits", "lower_bound_m",
    "upper_bound_m", "bound_scope", "solver_status", "returncode", "watchdog",
    "termination_status", "termination_exception", "termination_reason", "attempt", "result_file",
    "checkpoints_file", "attempt_issues",
)
METHOD_DIR = re.compile(r"^(\d+)-(.+)-(cvrp|vrptw|evrptw)$")
ATTEMPT_DIR = re.compile(r"^attempt-(\d+)$")


def read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def read_manifest(root):
    path = root / "manifest.json"
    if not path.exists():
        return {}, None
    manifest = read_json(path)
    inputs = {}
    for item in manifest["inputs"]:
        identifier = item["instance_id"]
        if identifier in inputs:
            raise ValueError(f"重复的清单实例 {identifier}")
        inputs[identifier] = item
    return manifest, inputs


def path_metadata(path):
    size_dir = Path(path).parent
    match = re.fullmatch(r"Cus(\d+)", size_dir.name, re.IGNORECASE)
    if match and size_dir.parent.name.lower() == "test1":
        return size_dir.parent.parent.name, int(match[1])
    return "", None


def read_result(folder, identifier, method, view, manifest, item):
    result = read_json(folder / "result.json")
    expected = dict(instance_id=identifier, method=method, view=view,
                    seconds=manifest.get("seconds", 1800), threads=1)
    if "seed" in manifest:
        expected["seed"] = manifest["seed"]
    if item is not None:
        expected["sha256"] = item["sha256"]
        if "path" in item:
            recorded = Path(item["path"])
            actual = Path(result["input"])
            names_match = actual == recorded if recorded.is_absolute() else (
                len(recorded.parts) > 0 and actual.parts[-len(recorded.parts):] == recorded.parts)
            if not names_match:
                raise ValueError("input 路径与清单不符")
    mismatches = [k for k, v in expected.items() if result.get(k) != v]
    if mismatches:
        raise ValueError("运行设置或身份不匹配 " + ", ".join(mismatches))
    if result.get("status") not in {"pass", "setup_error", "incompatible", "license_blocked", "execution_error",
                                   "replay_failed", "no_feasible_within_budget", "station_probe_failed"}:
        raise ValueError(f"无法识别 bench.py 终态 {result.get('status')!r}")
    return result


def read_checkpoints(path, seconds):
    rows = read_json(path)
    if not isinstance(rows, list):
        raise ValueError("检查点文件应为数组")
    deadlines = set()
    for row in rows:
        at = row["at_s"]
        best = row["best"]
        if not isinstance(at, (float, int)) or not 0 < at <= seconds or at in deadlines:
            raise ValueError(f"检查点时间错误 {at!r}")
        if best is not None:
            if not isinstance(best, dict) or not 0 <= best["elapsed_s"] <= at:
                raise ValueError(f"检查点 {at} 的路线时间错误")
            if best.get("distance_m") is None or best.get("vehicles") is None:
                raise ValueError(f"检查点 {at} 缺少回放结果")
        deadlines.add(at)
    return sorted(rows, key=lambda row: row["at_s"])


def select_attempt(root, folders, identifier, method, view, manifest, item):
    """Select the newest finalized attempt; keep paths to errors and earlier failures."""
    issues = []
    selected = fallback = None
    for number, folder in sorted(folders, reverse=True):
        relative = str(folder.relative_to(root))
        try:
            result = read_result(folder, identifier, method, view, manifest, item)
        except (OSError, ValueError, KeyError, TypeError) as err:
            issues.append(dict(attempt=relative, issue=str(err)))
            continue
        if result["status"] != "pass":
            issues.append(dict(attempt=relative, status=result["status"], reason=result.get("reason", "")))
        if selected is not None:
            continue
        checkpoints_path = folder / "checkpoints.json"
        checkpoints = []
        if result["status"] != "setup_error" or checkpoints_path.exists():
            try:
                checkpoints = read_checkpoints(checkpoints_path, result["seconds"])
            except (OSError, ValueError, KeyError, TypeError) as err:
                issues.append(dict(attempt=relative, issue=f"checkpoints.json: {err}"))
            expected = {t for t in (60, 300, 900, 1800) if t <= result["seconds"]} | {result["seconds"]}
            missing = expected - {row["at_s"] for row in checkpoints}
            if missing:
                issues.append(dict(attempt=relative, missing_checkpoints_s=sorted(missing)))
                if fallback is None:
                    fallback = number, folder, result, checkpoints
                continue
        selected = number, folder, result, checkpoints
    return selected or fallback, issues


def report(root, writer):
    manifest, inputs = read_manifest(root)
    methods = {}
    plan_path = root / "run-plan.json"
    if plan_path.exists():
        plan = read_json(plan_path)
        view = manifest.get("view", plan.get("view"))
        for method in plan["methods"]:
            pair = method, view
            if pair not in COMBINATIONS:
                raise ValueError(f"run-plan.json 的 solver/view 无法识别 {pair}")
            methods[pair] = COMBINATIONS.index(pair) + 1
    attempts = defaultdict(list)
    for folder in sorted(root.glob("*/*")):
        match = METHOD_DIR.fullmatch(folder.name)
        if not match or not folder.is_dir():
            continue
        identifier = folder.parent.name
        ordinal, method, view = match.groups()
        if inputs is not None and (identifier not in inputs or ("view" in manifest and view != manifest["view"])):
            print(f"清单外目录（已跳过） {folder.relative_to(root)}", file=sys.stderr)
            continue
        methods[method, view] = int(ordinal)
        key = identifier, method, view
        attempts.setdefault(key, [])  # An empty method directory is pending.
        for trial in folder.iterdir():
            trial_match = ATTEMPT_DIR.fullmatch(trial.name)
            if trial_match and trial.is_dir():
                attempts[key].append((int(trial_match[1]), trial))

    stats = defaultdict(Counter)
    ordered_methods = sorted(methods, key=methods.get)
    identifiers = inputs if inputs is not None else sorted({key[0] for key in attempts})
    keys = ((identifier, method, view) for identifier in identifiers for method, view in ordered_methods
            if inputs is not None or (identifier, method, view) in attempts)
    for identifier, method, view in keys:
        item = inputs[identifier] if inputs is not None else None
        chosen, issues = select_attempt(root, attempts[identifier, method, view],
                                         identifier, method, view, manifest, item)
        number, folder, result, checkpoints = chosen or ("", None, {}, [])
        city, size = path_metadata(result.get("input", ""))
        if item is not None:
            city, size = item["city"], int(item["n"])
        status = result.get("status", "pending")
        stats[method, view, city, size][status] += 1
        if writer is None:
            continue
        relative = folder.relative_to(root) if folder else None
        adapter = result.get("adapter") or {}
        termination = result.get("termination") or {}
        common = dict(
            city=city, size=size, slot=(item or {}).get("slot"), instance_id=identifier,
            sha256=result.get("sha256", (item or {}).get("sha256")), method=method, view=view,
            seed=result.get("seed", manifest.get("seed")), budget_s=manifest.get("seconds", 1800),
            status=status, reason=result.get("reason"), wall_s=result.get("wall_s"),
            first_feasible_s=result.get("first_feasible_s"), attempt=number,
            solver_status=adapter.get("solver_status") or adapter.get("model_status"),
            returncode=result.get("returncode"), watchdog=result.get("watchdog"),
            termination_status=termination.get("status"), termination_exception=termination.get("exception"),
            termination_reason=termination.get("reason"),
            result_file=relative / "result.json" if relative else None,
            checkpoints_file=relative / "checkpoints.json" if relative and (folder / "checkpoints.json").is_file() else None,
            attempt_issues=json.dumps(issues, ensure_ascii=False) if issues else "",
        )
        if not checkpoints:
            writer.writerow(common)
        for checkpoint in checkpoints:
            best = checkpoint["best"] or {}
            metrics = {k: best.get(k) for k in ("distance_m", "vehicles", "charging_visits")}
            bounds = {}
            if method == "gurobi":
                # A final lower bound can only be attached to the final checkpoint.
                last = adapter if checkpoint["at_s"] == result["seconds"] else {}
                incumbent = best.get("meta") or {}
                for field in ("lower_bound_m", "upper_bound_m", "bound_scope"):
                    bounds[field] = next((source[field] for source in (checkpoint, last, incumbent)
                                          if source.get(field) is not None), None)
                if bounds["upper_bound_m"] is None:
                    bounds["upper_bound_m"] = best.get("distance_m")
            writer.writerow(dict(common, **metrics, checkpoint_s=checkpoint["at_s"],
                                 incumbent_s=best.get("elapsed_s"), **bounds))
    if not stats:
        prefix = f"清单实例 {len(inputs)}，" if inputs is not None else ""
        print(prefix + "尚无可识别的方法运行目录。")
        return
    print("method\tview\tcity\tsize\ttotal\tstatus_counts")
    def order(group):
        method, view, city, size = group
        return methods[method, view], city, size or 0, method, view
    for group in sorted(stats, key=order):
        status_counts = " ".join(f"{s}={n}" for s, n in sorted(stats[group].items()))
        method, view, city, size = group
        print(method, view, city or "?", size or "?", stats[group].total(), status_counts, sep="\t")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="bench.py 运行根目录")
    parser.add_argument("--csv", type=Path, help="导出明细，每个实际检查点一行；无检查点的运行留空")
    args = parser.parse_args()
    root = args.out.resolve()
    if not root.is_dir():
        parser.error(f"运行目录不存在 {root}")
    try:
        if args.csv:
            target = args.csv.resolve()
            if target in {root / "manifest.json", root / "run-plan.json"} or (target.is_relative_to(root) and
                    any(ATTEMPT_DIR.fullmatch(part) for part in target.relative_to(root).parts)):
                parser.error("CSV 不能覆盖运行清单或写入 attempt 证据目录")
            with NamedTemporaryFile("w", dir=target.parent, encoding="utf-8", newline="",
                                    delete=False) as stream:
                temp = Path(stream.name)
            try:
                with temp.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=FIELDS)
                    writer.writeheader()
                    report(root, writer)
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
        else:
            report(root, None)
    except (OSError, ValueError, KeyError, TypeError) as err:
        parser.exit(2, f"报表生成失败 {err}\n")


if __name__ == "__main__":
    main()
