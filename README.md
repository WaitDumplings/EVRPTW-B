# EVRPTW-B

EVRPTW-B 在同一份 EVRPTW-D Parquet 上运行 CVRP、VRPTW 和 EVRPTW 求解器，并使用原始有向路网矩阵独立验证车辆路线。求解器登记表包含 9 种非学习型配置。Chicago 样例包含 C++ VNS/TS 在内的 12 个组合的 60 秒验收记录。[Chicago Cus100 验收样例](examples/chicago-cus100/README.md) · [完整精度 CSV](examples/chicago-cus100/summary.csv)

| 视图 | 非学习型方法 |
|---|---|
| CVRP | OR-Tools GLS、HGS-CVRP、PyVRP-ILS、Gurobi、LKH-3 |
| VRPTW | OR-Tools GLS、PyVRP-ILS、PyVRP-HGS、Gurobi、LKH-3（独立时间矩阵） |
| EVRPTW | VNS/TS、RoutingBlocks ALNS、HMA、Gurobi |

## 代码入口

- [求解器实现](adapters/non_learning/)包含经典车辆路径方法、电动车启发式和 Gurobi 模型。环境准备、原生编译和充电探针位于 [adapters/](adapters/)。
- [bench.py](bench.py) 负责单例和多例执行，[runtime.py](runtime.py) 登记方法与视图，[worker.py](worker.py) 控制求解进程和候选路线导出。
- [replay.py](replay.py) 在原始矩阵上独立回放，[summarize.py](summarize.py) 从本地运行生成验收表。
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

只准备 VNS/TS 时，直接编译仓库随附的单线程 C++17 源码。

```bash
"$PY" adapters/prepare_electric.py --vns-only
```

准备全部电动启发式和 Gurobi 时，需要能访问相应源码仓库的 Git 账号。

```bash
"$PY" adapters/prepare_electric.py
```

如需在多个检出之间复用已构建的环境和原生库，运行准备脚本和求解器前设置同一个 `EVB_DEPS=/absolute/path/to/deps`。

经典方法使用独立的 OR-Tools、PyVRP-ILS 和 PyVRP-HGS 环境。VNS/TS 使用本仓库的 C++ 实现。RoutingBlocks 和 HMA 使用道路版提交 `bd94474c551b39ac90e98a25a2cc43cb919f81b8`。[经典方法的版本和数值换算](adapters/CLASSICAL.zh-CN.md)

Gurobi 的组内模型及共享资源代码默认通过 SSH 获取。RoutingBlocks 和 HMA 的道路版默认通过 HTTPS 获取。可分别设置 `EVB_GROUP_REPO_URL` 与 `EVB_ROAD_REPO_URL` 指定镜像，准备脚本会检出固定提交。

Gurobi 验收使用 `gurobipy==13.0.2` 和本机 `maojie` 环境的有效许可。在另一台机器上，可用 `EVB_PYTHON_GUROBI` 指向已安装 Gurobi、NumPy、PyArrow 且具有相应规模许可的解释器。其余方法的解释器选择见 [runtime.py](runtime.py)。

各方法使用的作者仓库见 [CPU 接入任务书](EVRPTW-B_CPU_SOLVER_HANDOFF.zh-CN.md)。RoutingBlocks 和 HMA 的上游来源、固定提交及许可说明见[道路适配仓库的第三方声明](https://github.com/WaitDumplings/Benchmark/blob/bd94474c551b39ac90e98a25a2cc43cb919f81b8/THIRD_PARTY_NOTICES.md)。

LKH-3 使用独立准备脚本。CVRP 调用 3.0.10 原版，VRPTW 调用支持独立行驶时间矩阵的补丁版。[构建、第三方许可与来源记录](adapters/CLASSICAL.zh-CN.md#lkh-3)

## 本次输入

验收使用经确认的 Chicago `train/Cus100` 首槽。原始输入来自 EVRPTW-D 的 Stage 2 交付目录。复跑时将下方的 `DATA_ROOT` 设为该交付目录，或将 `INPUT` 指向持有的同一 Parquet。运行器会核对实例 ID 和 SHA-256。

```bash
ID=626c1a86d16d7c06ab213f62
SHA=7b9061e7ff08e1de2567c24a6b41d10a28ea6adb2c582418b09fd3ceb4aa4b91
DATA_ROOT="${DATA_ROOT:-../city-v2-output}"
INPUT="${INPUT:-$DATA_ROOT/instances/chicago/train/Cus100/$ID.parquet}"
args=(--input "$INPUT" --expect-id "$ID" --expect-sha256 "$SHA" --threads 1 --seed 20261103 --out acceptance)
```

## 单例复跑

以下 12 条按任务书顺序逐条执行。各组合均使用 `bench.py run` 的默认预算 60 秒。重复运行会创建新的 `attempt-NNN`，历史尝试保留。每条命令退出前已完成该项回放。

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

所有方法单核、单求解线程运行。公共读取器加载实例、工作进程导入适配器后，执行器用单调墙钟放行启发式方法。预算涵盖从适配器接收实例开始的矩阵转换、初始解构造及后续搜索。Gurobi 先构建模型并以 `model.update()` 落实变量和约束，将这段耗时单列在 `result.json` 的 `model_build_s`；随后启动求解预算，计入 MIP start 的构造、安装及优化。执行器沿用各方法的收尾余量，并在截止时终止仍在运行的求解进程组。仓库中较早的 Chicago 样例采用当时的计时协议。

候选携带完整节点序列写入 `events.jsonl`，时间戳取自提交给评测器的时刻。独立回放在搜索结束后读取原始节点、有向距离和对应的行驶时间矩阵，检查客户覆盖、载重、时间窗、回仓时刻，以及电动车的电量与逐站满充。时间、电量的比较容差为 `1e-8`，载量为 `1e-6 cm³`。回放和充电探针分别计时。

`checkpoints.json` 从一次运行的事件流中还原第 **60、300、600、900、1200、1500、1800 秒**的状态。每个时点保存截至该时刻已提交、回放可行且距离最短的完整路线和距离（米），暂无可行解时 `best` 为 `null`。提前完成的求解器沿用最终已知状态。短预算只记录预算内的标准时点及该次预算的终点。各时点的归档和回放均在搜索计时结束后完成，搜索进行中无需为时点暂停或重复求解。

Gurobi 检查点同时保存优化器当时已报告的下界 `lower_bound_m` 和上界 `upper_bound_m`。这两个字段独立于回放距离，各自在尚未取得时为 `null`。EV 模型为每个物理站设全车队共用的两个副本，优化器的界限适用于该受限模型。`routes.json` 保存预算内的最好可行路线，`replay.json` 保存其逐车逐弧的回放轨迹，`result.json` 记录实例身份、CPU、版本、方法墙钟、首个可行时间和终态。

也可仅用输入与导出的路线重做独立回放。以下命令检查输入哈希和实例 ID，再输出逐车逐弧轨迹。

```bash
"$PY" replay.py --input "$INPUT" --routes "examples/chicago-cus100/01-ortools-cvrp/routes.json" --trace
```

## 正式 Test1 评测

Chicago 和 Houston 的 Test1 共 300 例。下列命令按登记顺序运行 OR-Tools、HGS-CVRP、PyVRP-ILS、Gurobi 和 LKH，共 1,500 个 CVRP 求解任务。每个任务运行一次，预算为 30 分钟，记录第 1、5、10、15、20、25、30 分钟的最好可行解。

```bash
"$PY" scripts/run_test1.py --data-root ../city-v2-output --out ../test1-cvrp --view cvrp --plan-only
"$PY" scripts/run_test1.py --data-root ../city-v2-output --out ../test1-cvrp --view cvrp
```

每个方法先并行处理两城的 Cus100 和 Cus500，再处理 Cus1000。中断后用原命令和原结果目录续跑。并发数、方法选择、检查点和失败记录见 [Test1 运行说明](docs/formal-test1.zh-CN.md)。
