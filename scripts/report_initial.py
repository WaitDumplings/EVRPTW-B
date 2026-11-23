"""Summarize initial and final routes for the tasks in a formal-run manifest."""
import argparse
import csv
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from initial_record import INITIAL_PAIRS, matches_initial
from scripts.run_formal import completed_attempt, planned_pairs


def summarize(items):
    distances, gains, percent = [], [], []
    counts = dict(tasks=0, recorded=0, no_initial_solution=0, unknown=0, pending=0, feasible=0)
    for record, final in items:
        counts["tasks"] += 1
        if record is None:
            counts["pending"] += 1
            continue
        status = record["status"]
        counts[status] += 1
        if status != "recorded":
            continue
        d = record["distance_m"]
        distances.append(d)
        counts["feasible"] += int(record["feasible"])
        if final is not None:
            gains.append(d - final)
            if d > 0:
                percent.append(100 * (d - final) / d)
    def mean(values):
        return statistics.fmean(values) if values else None
    return dict(**counts, feasible_rate=counts["feasible"] / counts["tasks"],
                mean_initial_m=mean(distances), paired=len(gains), mean_improvement_m=mean(gains),
                paired_positive_initial=len(percent), mean_improvement_pct=mean(percent))


def rows(out, cfg):
    groups = {}
    for item in cfg["inputs"]:
        for method, view in planned_pairs(item, cfg["gurobi"], cfg.get("methods"), cfg.get("instance_ids")):
            if (method, view) not in INITIAL_PAIRS:
                continue
            key = (method, view, item["split"], item["n"])
            group = groups.setdefault(key, [])
            archived = completed_attempt(out, item, method, view, cfg)
            record = final = None
            if archived:
                attempt, status = archived
                target = attempt / "initial_solution.json"
                if target.exists():
                    result = json.loads((attempt / "result.json").read_text())
                    record = json.loads(target.read_text())
                    if (not matches_initial(record, result)
                            or (record["city"], record["split"], record["n"]) !=
                            (item["city"], item["split"], item["n"])):
                        raise ValueError(f"Initial record identity mismatch: {target}")
                    if status == "pass":
                        final = result["distance_m"]
            group.append((record, final))
    for (method, view, split, n), items in sorted(groups.items()):
        yield dict(method=method, view=view, split=split, n=n, **summarize(items))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="formal run root with manifest.json")
    args = parser.parse_args()
    cfg = json.loads((args.out / "manifest.json").read_text())
    writer = None
    for row in rows(args.out, cfg):
        if writer is None:
            writer = csv.DictWriter(sys.stdout, fieldnames=list(row), lineterminator="\n")
            writer.writeheader()
        writer.writerow(row)


if __name__ == "__main__":
    main()
