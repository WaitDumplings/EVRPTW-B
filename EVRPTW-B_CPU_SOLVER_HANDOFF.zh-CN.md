# EVRPTW-B 非学习型求解器接入任务

请以 code agent 身份完成以下工作。输入是一份已经生成的 Chicago Cus100 Parquet。请把同一份输入接入下表的 CPU 求解器，按顺序完成适配、执行和路线回放。第一版交付可重跑的代码和逐项结果。本轮每个“求解器 × 视图”只验证这一个实例，每次运行上限 **60 秒**。完成接入验收后停止，批量评测另行启动。

## 1. 计时和并行约定

| 设置 | 本轮接入验证 | 正式评测 |
| --- | --- | --- |
| 范围 | 同一份 Chicago Cus100，逐项接入 | CVRP、VRPTW、EVRPTW，各规模采用同一时限 |
| 每次运行 | 1 个物理核、1 个求解线程、60 秒墙钟上限 | 1 个物理核、1 个求解线程、1,800 秒墙钟上限 |
| 记录 | 首个可行解的时间，60 秒内最好可行路线 | 一次运行，在 60、300、900、1,800 秒记录截至当时的最好可行解 |
| 同时运行的作业 | 当前项跑通后接下一项 | 跨实例并行，例如 25 个可用物理核且内存足够时并行 25 个单核作业 |

从读好公共实例、把数据交给方法适配器时启动单调墙钟，覆盖该方法的矩阵转换、建模、初始化和搜索。把剩余时间传给 solver，及时保存带时间戳的最好解；外层执行器执行同一截止时间。下载、安装、编译和独立回放分别计时。Gurobi 显式设置 `Threads=1`，含隐式并行数学库的方法也限制为单线程。允许提前完成。每次使用一个固定并记录的搜索 seed。

## 2. 共用哪个实例

随本任务提供 `chicago-cus100-review.zip`，或直接提供其中的单文件 Parquet。测试实例 ID 为 `7e21cd8cd93f6dfcd657ca02`，文件 SHA-256 为 `93b3344800a6c6ed37cdf736e18cff3b693efbb52f94a31ef480e77ce63f5f40`。外层 ZIP 的根目录中已有该 Parquet；另一个 `instance0001.evrp.zip` 是生成器归档。使用现成 Parquet 即可，城市 CLE、Stage 1、Stage 2 无需重跑。

在接入项目外取得读取器，并在其仓库根目录运行下例。将 `INPUT` 替换为提供的 Parquet 绝对路径。仓库中的 `instance_file.read_instance` 支持直接选择视图。[EVRPTW-D-HF](https://github.com/WaitDumplings/EVRPTW-D-HF) · [读取器](https://github.com/WaitDumplings/EVRPTW-D-HF/blob/main/instance_file.py)

```bash
git clone https://github.com/WaitDumplings/EVRPTW-D-HF.git
cd EVRPTW-D-HF
python -m pip install numpy pyarrow
INPUT=/absolute/path/to/7e21cd8cd93f6dfcd657ca02.parquet
python - "$INPUT" <<'PY'
import json
import sys
from instance_file import read_instance

for view in ("cvrp", "vrptw", "evrptw"):
    task = read_instance(sys.argv[1], view)
    vehicle = json.loads(task.info["vehicle_json"])
    print(view, task.info["instance_id"], task.nodes.num_rows,
          list(task.matrices), vehicle["cargo_capacity_cm3"])
PY
```

| 视图 | 本例输入 | 求解与独立回放使用的规则 |
| --- | --- | --- |
| CVRP | 1 depot + 100 客户，101 × 101 有向距离矩阵 | 同仓往返，每个客户恰服务一次，整单配送，每车一趟，载货量不超过容量。 |
| VRPTW | 同一批节点，101 × 101 有向距离和时间矩阵 | 加入客户服务时长、硬服务开始窗和回仓班次；早到可等待，时间窗两端均包含。 |
| EVRPTW | 再加 10 座 CS，共 111 个节点、4 张 111 × 111 矩阵 | 在 VRPTW 规则上逐弧传播电量，到达时满足保留电量，进站按该站有效功率线性充满；允许重复访站，depot 只作起终点。 |

三个视图统一最小化总有向道路距离。路线打分取原始 float64 米数 `shortest_distance_m[i,j]`；VRPTW 和 EVRPTW 的行驶时间取**同一最短距离路径**的 `shortest_distance_time_s[i,j]`。EV 文件也含最快路径的距离和时间，本轮用前一对矩阵行驶。需求与容量用 cm³，时间为当地午夜起秒数，车辆参数从 `vehicle_json` 读取。每条弧耗电为米数除以 1,000 再乘 `specific_energy_kwh_per_km`，进站时长为 `3600 × 补充电量(kWh) / charge_kw`，其中 `charge_kw` 已是有效功率。车队数无硬上限；需要显式给车数的接口可用客户数作为不收紧可行域的上限，按总距离选解，车辆固定成本取零。需要整数的接口记录换算比例，最终以原矩阵及原始节点属性回放。[数据格式与字段](https://github.com/WaitDumplings/EVRPTW-D-HF/blob/main/docs/OUTPUT.zh-CN.md)

## 3. 要接的仓库和执行顺序

表中一行可包含两个或三个视图，每个视图各运行一次。共 **9 项启发式接入、3 项精确模型接入**。先完成共享输入与路线回放，再按表逐行推进。记录实际使用的提交号或发布版本。

| 顺序 | 求解器 | 逐项运行的视图 | 源码与接入要点 |
| --- | --- | --- | --- |
| 1 | OR-Tools GLS | CVRP、VRPTW | 使用 Routing Solver 的 `GUIDED_LOCAL_SEARCH`。距离成本与时间传播分别注册，时间传播包含服务时长。[google/or-tools](https://github.com/google/or-tools) |
| 2 | HGS-CVRP | CVRP | 调用已有显式有向距离矩阵接口；坐标用于接口要求的空间信息，弧成本来自实例。[作者仓库](https://github.com/vidalt/HGS-CVRP) |
| 3 | PyVRP-ILS | CVRP、VRPTW | 用 v0.14.0，为每个方向提供相应距离和需要的行驶时间。[PyVRP v0.14.0](https://github.com/PyVRP/PyVRP/releases/tag/v0.14.0) |
| 4 | PyVRP-HGS | VRPTW | 用单独环境载入 v0.9.0，复用相同的原始 VRPTW 输入。[PyVRP v0.9.0 搜索入口](https://github.com/PyVRP/PyVRP/blob/v0.9.0/pyvrp/solve.py) |
| 5 | VNS/TS | EVRPTW | 用本组已有的 Python 版本，保留既有邻域和搜索策略。[固定提交的运行入口](https://github.com/WaitDumplings/EVRPTW_DB_ICLR/blob/09ffe9dba09f9c21c70be3590e721b0a605ffec4/EVRPTW_Benchmark/MetaHeuristics/VNS_TS_Solver/run_vns_ts.py) |
| 6 | RoutingBlocks EVRPTW-ALNS | EVRPTW | 用完整的 `examples/evrptw` 或本组基于它的既有适配版。上游示例采用部分充电，优先复用已有满充、逐站功率的资源计算。[框架与完整示例](https://github.com/tumBAIS/RoutingBlocks/tree/main/examples/evrptw) |
| 7 | HMA | EVRPTW | 使用作者 C++ 搜索及充电站插入程序，优先复用本组已有的道路版接入。核对实际启用的距离目标、满充与逐站功率；原文题型包含部分充电及同时取送。[0SliverBullet/EVRP-TW-SPD-HMA](https://github.com/0SliverBullet/EVRP-TW-SPD-HMA) |
| 8 | Gurobi MILP | CVRP、VRPTW、EVRPTW | 从本组既有模型接起。按对应视图建模，CVRP 使用容量和路径约束，VRPTW 加时间，EVRPTW 再加能量和站点。沿用本机或服务器已有的有效许可，记录求解状态、可行解及可用的界。[本组精确模型入口](https://github.com/WaitDumplings/EVRPTW_DB_ICLR/blob/09ffe9dba09f9c21c70be3590e721b0a605ffec4/EVRPTW_Benchmark/Exact/Gurobi_Solver/run_gurobi.py) |

本组 VNS/TS 和 Gurobi 均可从 `https://github.com/WaitDumplings/EVRPTW_DB_ICLR.git` 的 `09ffe9dba09f9c21c70be3590e721b0a605ffec4` 取得。HMA 与 RoutingBlocks 表中给出上游入口；若机器上有本组旧 benchmark 的资源评估代码，同时记录它的来源和本次改动。适配限于输入、目标和资源规则的接线、时限、日志、路线输出；沿用现有搜索和充电决策方法。若必须重新设计搜索或充电算法才能支持题设，留下具体缺口与源码位置，继续其余项。

旧 Gurobi EV 模型限制每座物理站的拷贝数。它输出的路线可按原实例验证并给出可行上界；若使用了这项限制，记录拷贝数并将模型下界或最优声明明确标为受限模型结果。60 秒接入验收要求可行解，最优性证明另行报告。

## 4. 怎样算跑通，交付什么

1. 每个适配器提供一条可直接复制的运行命令，接受输入 Parquet、视图、时限、线程数和输出目录。按源文件的 `node_idx` 返回所有车辆的完整节点序列，保留 depot 与沿途每次 CS 访问。若 solver 重排节点，将编号映射回输入。
2. 只有当该方法在 60 秒内实际返回路线、且这份路线通过独立回放，才把对应视图记为 **通过**。回放重算总米数，检查客户覆盖和容量，对含时间与电量的视图增加相应检查。合法的零充电方案可以通过。若该解没有访站，另用本例一段真实弧和站点核对适配器的耗电、补电时长及离站电量，并注明该段检查与求解结果的区别。
3. 若输入、编号、单位或计时有误，修正后在原实例、原 seed、同一 60 秒上限重试。超时无可行解、缺少许可、协议不兼容、回放失败分别记录。故障定位后继续独立条目，最后回到尚可修复的项。保留真实状态和必要的失败日志。
4. 交付改动后的代码、可复跑命令、每项的路线与回放结果，以及一张 **12 行汇总表**。表内至少含视图、求解器及版本、状态、实际墙钟秒数、首次可行时间、独立回放总米数、车数、充电访问次数（EV）、失败原因或结果文件。精确法另报适用模型的上下界和终止状态。注明本轮实例 ID、输入哈希及实际线程数。完成后汇报通过数及剩余问题，即结束本轮。
