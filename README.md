# EVRPTW-B

EVRPTW-B 在同一份 EVRPTW-D Parquet 上运行 CVRP、VRPTW 和 EVRPTW 求解器，并使用原始有向路网矩阵独立验证车辆路线。当前发布准备包含 8 种非学习型求解器配置、12 个已验收的“求解器 × 视图”组合。[Chicago Cus100 验收样例](examples/chicago-cus100/README.md) · [完整精度 CSV](examples/chicago-cus100/summary.csv)

| 视图 | 非学习型方法 |
|---|---|
| CVRP | OR-Tools GLS、HGS-CVRP、PyVRP-ILS、Gurobi |
| VRPTW | OR-Tools GLS、PyVRP-ILS、PyVRP-HGS、Gurobi |
| EVRPTW | VNS/TS、RoutingBlocks ALNS、HMA、Gurobi |

## 代码入口

- [求解器实现](adapters/non_learning/)包含经典车辆路径方法、电动车启发式和 Gurobi 模型。环境准备、原生编译和充电探针位于 [adapters/](adapters/)。
- [bench.py](bench.py) 负责单例和多例执行，[runtime.py](runtime.py) 登记方法与视图，[worker.py](worker.py) 控制求解进程和候选路线导出。
- [replay.py](replay.py) 在原始矩阵上独立回放，[summarize.py](summarize.py) 从完整本地运行生成 12 行验收表。
- [CPU 接入任务书](EVRPTW-B_CPU_SOLVER_HANDOFF.zh-CN.md)记录各方法的上游来源、视图和验收要求。

## 准备运行环境

从 EVRPTW-B 仓库根目录运行。本机已有固定提交的读取器和运行环境，可直接选择它们。

```bash
export EVRPTWD_READER="$(realpath ../EVRPTW-D-HF-city-v2)"
PY="$EVRPTWD_READER/.venv/bin/python"
```

在新的 Linux 检出目录中，以下命令建立 Python 3.11 控制环境并取得固定提交的读取器。准备原生方法还需 Git 和 C++17 编译器。

```bash
python3.11 -m venv .venv
PY="$PWD/.venv/bin/python"
"$PY" -m pip install numpy==2.4.6 pyarrow==26.0.1
mkdir -p .deps
git clone https://github.com/WaitDumplings/EVRPTW-D-HF.git .deps/reader
git -C .deps/reader checkout --detach e2cf4825dd6a7c4ecd2cf88bef6cb206908ae50e
```

新检出默认使用项目内的 `.deps/reader`。已有 EVRPTW-D 读取器时，可通过 `EVRPTWD_READER` 指定其绝对路径。六项经典方法可单独安装，依赖均来自公开来源。

```bash
"$PY" adapters/prepare_classical.py
```

三项电动启发式和 Gurobi 使用组内固定版本的源码，需 Git 账号能访问相应仓库。

```bash
"$PY" adapters/prepare_electric.py
```

如需在多个检出之间复用已构建的环境和原生库，运行准备脚本和求解器前设置同一个 `EVB_DEPS=/absolute/path/to/deps`。

经典方法使用独立的 OR-Tools、PyVRP-ILS 和 PyVRP-HGS 环境。电动方法使用 VNS/TS 的固定组内提交 `09ffe9dba09f9c21c70be3590e721b0a605ffec4` 和道路版 RoutingBlocks、HMA 提交 `bd94474c551b39ac90e98a25a2cc43cb919f81b8`。[经典方法的版本和数值换算](adapters/CLASSICAL.zh-CN.md)

VNS/TS 和 Gurobi 的组内源码默认通过 SSH 获取，RoutingBlocks、HMA 的道路版默认通过 HTTPS 获取。使用 HTTPS 或其他 Git 镜像时，可分别设置 `EVB_GROUP_REPO_URL`（VNS/TS 和 Gurobi 源码）与 `EVB_ROAD_REPO_URL`（RoutingBlocks 和 HMA 道路版）。准备脚本会检出规定的提交。

Gurobi 验收使用 `gurobipy==13.0.2` 和本机 `maojie` 环境的有效许可。在另一台机器上，可用 `EVB_PYTHON_GUROBI` 指向已安装 Gurobi、NumPy、PyArrow 且具有相应规模许可的解释器。其余方法的解释器选择见 [runtime.py](runtime.py)。

各方法使用的作者仓库见 [CPU 接入任务书](EVRPTW-B_CPU_SOLVER_HANDOFF.zh-CN.md)。RoutingBlocks 和 HMA 的上游来源、固定提交及许可说明见[道路适配仓库的第三方声明](https://github.com/WaitDumplings/Benchmark/blob/bd94474c551b39ac90e98a25a2cc43cb919f81b8/THIRD_PARTY_NOTICES.md)。

## 本次输入

验收使用经确认的 Chicago `train/Cus100` 首槽。原始输入来自 EVRPTW-D 的 Stage 2 交付目录。复跑时将下方的 `DATA_ROOT` 设为该交付目录，或将 `INPUT` 指向持有的同一 Parquet。运行器会核对实例 ID 和 SHA-256。

```bash
ID=626c1a86d16d7c06ab213f62
SHA=7b9061e7ff08e1de2567c24a6b41d10a28ea6adb2c582418b09fd3ceb4aa4b91
DATA_ROOT="${DATA_ROOT:-../city-v2-output}"
INPUT="${INPUT:-$DATA_ROOT/instances/chicago/train/Cus100/$ID.parquet}"
args=(--input "$INPUT" --expect-id "$ID" --expect-sha256 "$SHA" --seconds 60 --threads 1 --seed 20261103 --out acceptance)
```

## 单例复跑

以下 12 条按任务书顺序逐条执行。重复运行会创建新的 `attempt-NNN`，历史尝试保留。每条命令退出前已完成该项回放。

```bash
"$PY" bench.py run --solver ortools       --view cvrp   "${args[@]}"
"$PY" bench.py run --solver ortools       --view vrptw  "${args[@]}"
"$PY" bench.py run --solver hgs           --view cvrp   "${args[@]}"
"$PY" bench.py run --solver pyvrp-ils     --view cvrp   "${args[@]}"
"$PY" bench.py run --solver pyvrp-ils     --view vrptw  "${args[@]}"
"$PY" bench.py run --solver pyvrp-hgs     --view vrptw  "${args[@]}"
"$PY" bench.py run --solver vns-ts        --view evrptw "${args[@]}"
"$PY" bench.py run --solver routingblocks --view evrptw "${args[@]}"
"$PY" bench.py run --solver hma           --view evrptw "${args[@]}"
"$PY" bench.py run --solver gurobi        --view cvrp   "${args[@]}"
"$PY" bench.py run --solver gurobi        --view vrptw  "${args[@]}"
"$PY" bench.py run --solver gurobi        --view evrptw "${args[@]}"
"$PY" summarize.py "acceptance/$ID"
```

## 计时和回放

执行器通过 Linux CPU 亲和性将每例的调度线程、求解进程及其子进程固定到一个逻辑 CPU，按插槽与物理核号区分物理核。数学库与求解器使用一个求解线程。公共输入读取完成后，父进程以单调时钟设定绝对截止时间，才向子进程放行。适配器导入、矩阵换算、建模、初始化和搜索均计入预算。方法收到扣除少量收尾余量的剩余时间；超限时父进程终止整个求解进程组。

候选路线及其时间戳即时写入 `events.jsonl`。回放独立于搜索，在计时结束后重新读取原始节点属性，用 `shortest_distance_m` 计价，用同一路径的 `shortest_distance_time_s` 传播行驶时间。回放检查客户恰好一次、载货、含等待与服务时长的硬窗、回仓时刻。EV 还检查每弧抵达后的电量下限和按本站有效 kW 线性充满，允许重复访问同一站。数值比较容差为时间和电量各 `1e-8`、载量 `1e-6 cm³`。

每次尝试的 `routes.json` 为预算内独立回放可行且原始总米数最小的已返回方案；`replay.json` 逐车逐弧保存回放轨迹。`result.json` 记录输入身份、CPU、版本、首个可行时间、墙钟时间、实际进程退出延迟和终态。`checkpoints.json` 按各时间点回溯当时已有的最好可行路线。EV 结果无访站时，另见 `station_probe.json` 中真实有向弧的耗电、充电时长和离站电量对照。探针不计为求解路线中的访站。Gurobi 的 MIP start 在计时内构造，路线以 Gurobi 接受的 incumbent 为准；EV 模型为每个物理站设全车队共用的两个副本，该模型给出的下界和最优性仅适用于此副本限制。

也可仅用输入与导出的路线重做独立回放。以下命令检查输入哈希和实例 ID，再输出逐车逐弧轨迹。

```bash
"$PY" replay.py --input "$INPUT" --routes "examples/chicago-cus100/01-ortools-cvrp/routes.json" --trace
```

## 正式 Test1 评测

Chicago 和 Houston 的 Test1 共 300 例。本轮在 CVRP 视图上依次运行 OR-Tools、HGS-CVRP、PyVRP-ILS 和 Gurobi，共 1,200 个求解任务。每个任务运行一次，预算为 30 分钟，记录第 1、5、15、30 分钟的最好可行解。

```bash
"$PY" scripts/run_test1.py --data-root ../city-v2-output --out ../test1-cvrp --view cvrp --plan-only
"$PY" scripts/run_test1.py --data-root ../city-v2-output --out ../test1-cvrp --view cvrp
```

每个方法先并行处理两城的 Cus100 和 Cus500，再处理 Cus1000。中断后用原命令和原结果目录续跑。并发数、方法选择、检查点和失败记录见 [Test1 运行说明](docs/formal-test1.zh-CN.md)。
