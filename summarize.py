"""Build the acceptance report from the latest real attempt for each combination."""
import argparse
import csv
import json
import math
from pathlib import Path

from runtime import COMBINATIONS


def fmt(value):
    return "—" if value is None else f"{value:,.3f}"


def link(label, relative_path):
    return f"[{label}]({relative_path})" if relative_path else ""


def record(base, ordinal, method, view):
    folder = base / f"{ordinal:02d}-{method}-{view}"
    attempts = sorted(folder.glob("attempt-*/result.json"),
                      key=lambda path: int(path.parent.name.split("-")[-1]))
    if not attempts:
        raise ValueError(f"missing actual run for {method}/{view}")
    file = attempts[-1]
    r = json.loads(file.read_text())
    settings = r["instance_id"], r["method"], r["view"], r["threads"]
    if settings != (base.name, method, view, 1) or not math.isfinite(r["seconds"]) or r["seconds"] <= 0:
        raise ValueError(f"wrong run settings in {file}")

    def artifact(name):
        path = file.parent / name
        return str(path.relative_to(base)) if path.is_file() else ""

    meta = r.get("adapter", {})
    version = meta.get("version") or meta.get("solver_version") or meta.get("source_commit")
    return dict(order=ordinal, method=method, version=version, view=view, status=r["status"],
                time_limit_s=r["seconds"], wall_s=r.get("wall_s"), model_build_s=r.get("model_build_s"),
                first_feasible_s=r.get("first_feasible_s"),
                best_s=r.get("best_s"), distance_m=r.get("distance_m"), vehicles=r.get("vehicles"),
                charging_visits=r.get("charging_visits"), lower_bound_m=meta.get("lower_bound_m"),
                upper_bound_m=meta.get("upper_bound_m"), bound_scope=meta.get("bound_scope"),
                solver_status=meta.get("solver_status") or meta.get("model_status"),
                seed=r["seed"], threads=r["threads"], socket=r["socket"],
                physical_core=r["physical_core"], cpu=r["cpu"], input_sha256=r["sha256"],
                replay_s=r.get("replay_s"), reason=r.get("reason", ""), result=artifact("result.json"),
                routes=artifact("routes.json"), replay=artifact("replay.json"),
                charge_probe=artifact("station_probe.json"), checkpoints=artifact("checkpoints.json"))


def markdown_row(row):
    version = row["version"] or "—"
    if len(version) == 40:
        version = version[:12]
    milp = "—"
    if row["method"] == "gurobi":
        scope = "每站 2 副本" if row["view"] == "evrptw" else "完整模型"
        milp = (f'{row["solver_status"] or "—"}，{scope}，'
                f'{fmt(row["lower_bound_m"])} / {fmt(row["upper_bound_m"])}')
    links = " ".join(filter(None, [link("结果", row["result"]), link("路线", row["routes"]),
                                   link("回放", row["replay"]), link("充电探针", row["charge_probe"])]))
    fields = [row["order"], row["method"], version, row["view"].upper(), row["status"],
              fmt(row["wall_s"]), fmt(row["first_feasible_s"]), fmt(row["distance_m"]),
              row["vehicles"] if row["vehicles"] is not None else "—",
              row["charging_visits"] if row["charging_visits"] is not None else "—", milp,
              " ".join(filter(None, [row["reason"], links]))]
    return "| " + " | ".join(str(item).replace("|", "\\|") for item in fields) + " |"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance_folder", type=Path)
    base = parser.parse_args().instance_folder.resolve()
    records = []
    for i, (method, view) in enumerate(COMBINATIONS, 1):
        # The original acceptance suite predates the two optional LKH runs.
        if method == "lkh" and not (base / f"{i:02d}-{method}-{view}").exists():
            continue
        records.append(record(base, i, method, view))
    if len({(r["input_sha256"], r["seed"], r["time_limit_s"]) for r in records}) != 1:
        raise ValueError("runs used different inputs, seeds or time budgets")
    with (base / "summary.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=records[0])
        writer.writeheader()
        writer.writerows(records)
    count = sum(r["status"] == "pass" for r in records)
    intro = [
        "# Chicago Cus100 单例验收", "",
        f"通过 {count}/{len(records)}。实例 `{base.name}`，SHA-256 `{records[0]['input_sha256']}`。"
        f"每项 1 个物理核、1 个求解线程、{records[0]['time_limit_s']:g} 秒墙钟上限，seed 为 `{records[0]['seed']}`。", "",
        "| 序号 | 求解器 | 版本 / 提交 | 视图 | 状态 | 墙钟秒 | 首个可行秒 | 回放距离 m | 车辆 | 访站 | Gurobi 终态、适用模型、下界 / 上界 m | 文件或原因 |",
        "|---:|---|---|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    notes = ["", "启发式墙钟包括适配、初始化和搜索。Gurobi 的建模耗时见 CSV 中的 model_build_s。"
             "有建模耗时记录的运行从建模完成后计时，早期协议将建模计入预算。"
             "路线距离由公共评测器在预算结束后独立回放，充电探针另行计时。"
             "Gurobi EV 模型的两个副本由全车队共用，表中 EV 界适用于这个限制。", "",
             "[完整精度 CSV](summary.csv)"]
    (base / "summary.md").write_text("\n".join([*intro, *map(markdown_row, records), *notes]) + "\n")
    print(base / "summary.csv")


if __name__ == "__main__":
    main()
