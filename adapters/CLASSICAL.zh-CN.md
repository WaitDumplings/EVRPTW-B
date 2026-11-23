# CPU 经典方法

从仓库根目录运行，`PY` 为 [README](../README.md) 中配置的 Python 3.11 解释器。需要 Git 和支持 C++17 的编译器。

```bash
"$PY" adapters/prepare_classical.py
```

| 适配项 | 执行环境 | 固定版本或提交 |
|---|---|---|
| OR-Tools GLS，CVRP / VRPTW | `.deps/ortools/bin/python` | 9.15.6755 |
| HGS-CVRP，CVRP | `.deps/ortools/bin/python` 加 `.deps/hgs-build/libhgs_observed.so` | 1a927955cd2861a29d978f0d359d6e647db9319c |
| PyVRP ILS，CVRP / VRPTW | `.deps/pyvrp-ils/bin/python` | 0.14.0 |
| PyVRP HGS，VRPTW | `.deps/pyvrp-hgs/bin/python` | 0.9.0 |

三套环境都安装 NumPy 2.4.6 和 PyArrow 26.0.1。准备脚本分别记录创建环境、下载 wheel、离线安装、首次拉取 HGS 和编译共享库的用时，默认写入 `.deps/preparation-times.json`。本机首次 pip 安装将下载和安装放在同一进程，首次 Git 拉取也未单独计时。独立复测在 `.deps/preparation-measured` 中新建环境、拉取相同的 HGS 提交并编译，计时见该目录的 `preparation-times.json`。运行器单独记录独立回放用时。

| 本机独立复测（秒） | 新建环境 | 下载 wheel | 离线安装 | Git 拉取并检出 | 编译 |
|---|---:|---:|---:|---:|---:|
| OR-Tools | 2.103 | 6.198 | 6.292 | — | — |
| PyVRP ILS | 2.092 | 6.806 | 4.944 | — | — |
| PyVRP HGS | 2.096 | 5.921 | 4.953 | — | — |
| HGS-CVRP | 使用 OR-Tools 环境 | — | — | 0.804 | 5.900 |

设 `INPUT` 为目标 Parquet 的绝对路径，以单线程分别执行六项。运行器从表中选择对应的解释器。示例把结果写入 `repeat/`。

```bash
set -eu
: "${INPUT:?set INPUT to the input Parquet}"
for item in ortools/cvrp ortools/vrptw hgs/cvrp pyvrp-ils/cvrp pyvrp-ils/vrptw pyvrp-hgs/vrptw; do
    method=${item%/*}
    view=${item#*/}
    "$PY" bench.py run \
        --solver "$method" --view "$view" --input "$INPUT" --threads 1 \
        --seconds 60 --seed 20261103 --out "repeat/$method/$view"
done
```

上述六项把客户数作为可用车辆上限，并保留零固定成本。OR-Tools 和两版 PyVRP 把每个方向的距离就近换算为毫米，时间用毫秒，对行驶、服务、窗开始向上取整，对窗结束向下取整。容量和需求使用每 cm³ 1,000 个整数单位，需求向上、容量向下取整。VRPTW 在客户的服务开始时刻施加硬窗，班次同时约束离仓与回仓。最终距离与可行性由公共回放器使用原始浮点字段计算。

HGS 通过作者的 `solve_cvrp_dist_mtx` 接收按行存放的有向 double 矩阵，用 km 和 L 表示距离和体积。坐标供原搜索使用。构建脚本为作者首次及历次全局可行最优的赋值加上路线通知，并将其循环时限接到单调墙钟。OR-Tools 在 Routing Solver 的解事件中提取路线，PyVRP 0.14 通过 ILS 的初始解和最优解回调提取路线，0.9 观察原 GA 的最优解赋值。两版各自在原搜索入口运行。

源节点 0 是 depot，客户依 Parquet 顺序注册。OR-Tools 用索引管理器还原节点，PyVRP 0.14 通过每个客户 activity 对应的 location 还原，0.9 和 HGS 的客户编号与源索引相同。每条路线都补全起终 depot，并在搜索进行中写入公共事件流。

## VNS/TS

方法 `vns-ts` 用于 EVRPTW 视图。仓库中的单线程 C++17 实现源自提交 `7e646c0062794a26184dfb55ac00840a49879ce8`，复现 Schneider、Stenger 和 Goeke（2014）的 VNS/TS。[发表版论文](https://doi.org/10.1287/trsc.2013.0490)

```bash
"$PY" adapters/prepare_electric.py --vns-only
```

准备脚本以 `-std=c++17 -O2` 编译 `adapters/vns_ts_cpp/vns_ts.cpp`，默认可执行文件为 `.deps/ev/vns_ts`，编译器版本、命令、源码和产物的 SHA-256 记录在 `.deps/ev/vns_ts-build.json`。适配器写出显式有向矩阵和车辆、节点资源，通过 `--seed`、`--deadline-monotonic` 和 `--events` 设置种子、截止时间和事件文件。首个可行解记为 `feasibility`，后续严格改进记为 `distance`。最终距离和可行性由公共回放器核对。充电探针通过 `--probe` 核对有向弧耗电、充满时间和离站电量。

**定稿参数。** 适配器显式传入两个非默认搜索参数 `--granular 30 --init-time-check 0`，原生程序默认分别为 0 和 1。仅在 9 个验证实例上比较近邻数 0、30、50、100 与初始时间检查 0、1 的八种组合。Chicago、New York、San Diego 在 Cus100、Cus500、Cus1000 各提供 1 例，每个规模 3 例，统一使用种子 `20261103`。Cus100 预算为 300 秒，另两个规模为 1,800 秒。选定同一组合用于所有规模，未做进一步调参。

论文表 4 的迭代参数照用原值，分别为可行性阈值 500、距离阶段 200 次 VNS、每次扰动最多 100 次 TS。在 500 和 1000 客户的验证比较中，48 组有 36 组在 1,800 秒内未完成一整轮（可行性阶段及后续 200 次距离迭代）。Cus500 完成 12/24 组，Cus1000 完成 0/24 组。单看定稿组合则分别完成 3/3 和 0/3 组。未据此调整迭代参数。

**与论文的差异。** 各项实现细节和对应代码见[原生实现说明](vns_ts_cpp/README.zh-CN.md#相对论文的改动和简化)。

1. 目标仅计总距离，车辆固定成本为零。论文先减少用车，再减少距离。
2. 保留空路线供搜索开车。移动及扰动后清空没有客户的路线。可行性阶段可依据已保存的全局可行解结束，车辆数无需等到第 500 次迭代才增加。
3. 初始构造可选择时间窗开线检查。定稿值 0 沿用论文的容量和无补能往返电量开线条件，客户插入仍按窗开始顺序。
4. 路网成本、行驶时间和电量按有向弧给定，电量使用扣除保留电量后的可用容量，满充时间按各站有效功率计算。
5. 容量、时间窗、电量的违反量换算成距离当量，再施加表 4 的动态罚权。
6. 在容量、时间和电量预处理后，按各顶点的出弧距离截取 30 个近邻，并补回通过预处理的仓库和站点弧。
7. 循环交换的参与路线数受实际非空路线数限制，只有一条配送路线时可用空路线配对。`stationInRe` 仅以“站点到客户”的生成弧在该客户之前插站或删站。
8. 禁忌属性简化为“有向弧、路线”，同一路线内的不同充电区间共用禁忌属性。
9. 多样化属性简化为“顶点、路线”，按本轮已执行的 TS 移动数归一，按受影响路线中新增的属性计罚。
10. 备选移动逐节点重算受影响路线的资源与距离。一次 TS 搜索可以在无允许移动或墙钟截止时提前结束。
11. 以方法墙钟为预算，完整轮结束后重新构造初解并保留全局最佳。正式结果取截止前已交付并通过公共回放的路线。

**保真度。** 在 Schneider 等人公开的 92 个实例上，以原生默认搜索参数（`--granular 0 --init-time-check 1`）各跑一整轮，对照发表版表 5、表 6 中作者十次运行的最好成绩。31 个同车数实例的平均距离差为 **+0.098%**，其中 27 个在论文的两位小数精度下相同，4 个较长。本实现只优化距离，其余 61 例均比论文多用车，单列车数及距离。5,247 条原生候选全部通过公共回放。[逐实例保真度结果](../docs/FIDELITY.md)

## LKH-3

| 方法和视图 | 来源与实现 | 可执行文件 | 传给 LKH 的车辆上限 `k` |
|---|---|---|---|
| `lkh/cvrp` | Keld Helsgaun 的 LKH-3.0.10 原版，题型 `ACVRP` | `.deps/lkh-3.0.10/bin/LKH` | 容量装箱车数 `b` 加 2 |
| `lkh/vrptw` | **B 级**。在同一发行版上增加独立的有向行驶时间输入，题型 `CVRPTW` | `.deps/lkh-3.0.10/bin/LKH-vrptw-time` | 可行构造车数 `g` 加上 `max(2, ceil(g/4))` |

作者保留代码权利，LKH-3 仅供学术及非商业使用。准备脚本校验原始发行包的 SHA-256 `c7432409e359f4b712983be8670b351b20875de1be93d7b1a074c45e1744f910`，分别编译原版和补丁版，并记录构建用时和校验值。补丁 SHA-256 为 `0472cec9a8036f9b17e3d93a43025150c99732839e88397b09ed2eae3ff902a4`。构建需要 C 编译器、GNU make 和 patch；运行需要 Linux、GNU time (`/usr/bin/time`) 及 GNU coreutils 的 `timeout`。[作者发布页存档](https://web.archive.org/web/20240630232626id_/http://webhotel4.ruc.dk/~keld/research/LKH-3/) · [作者发行包](http://akira.ruc.dk/~keld/research/LKH-3/LKH-3.0.10.tgz) · [时间矩阵补丁](patches/lkh-3.0.10-vrptw-time.patch)

```bash
"$PY" adapters/prepare_lkh.py
"$PY" bench.py run --solver lkh --view cvrp  --input "$INPUT" --threads 1 --seconds 60 --seed 20261103 --out repeat/lkh
"$PY" bench.py run --solver lkh --view vrptw --input "$INPUT" --threads 1 --seconds 60 --seed 20261103 --out repeat/lkh
```

两个视图均以有向距离为目标，输入距离取最近的整数分米。载量每立方厘米使用 1 个整数刻度，逐客户向上取整需求，向下取整车辆容量。取整后的容量和单点需求须能用 LKH 的 32 位有符号整数表示。试验级和运行级的候选在搜索中写入公共事件流，最终距离、用车数和可行性按原始浮点数据独立回放。

CVRP 按整数需求从大到小依次放入第一辆放得下的车，得到装箱车数 `b`。VRPTW 构造满足容量、客户时间窗、服务时间和仓库班次的路线，得到车数 `g`。VRPTW 按客户最晚时刻、最早时刻和节点编号依次处理客户。每次选择有向距离增量最小的可行插入位置，增量相同则依路线创建顺序和位置决定。已有路线均无法插入时才开新路线。构造在给 LKH 的整数时间和载量上检查插入，并在原始浮点数据上回放完整方案。构造只确定 `VEHICLES`，LKH 仍自行生成初始解。两个视图均允许空路线。车辆数构造、输入生成、子进程启动和路线提取均计入方法墙钟预算。

`result.json` 的 `adapter.vehicle_bound_basis` 和 `adapter.vehicle_bound_basis_vehicles` 记录构造类型及车数，`adapter.vehicle_bound_construction_s` 为构造用时，`adapter.vehicle_upper_bound` 为交给 LKH 的上限。顶层 `vehicles` 记录独立回放选出的最好可行解的用车数，`vehicle_bound_reached` 表示该解是否用满车辆上限。预算内未得到可行解时，触顶状态为 `null`。

正式评测关闭系统调用跟踪和采样剖析。`native_user_cpu_s`、`native_system_cpu_s` 与 `native_peak_rss_kib` 均由 GNU time 在子进程退出时汇总，统计范围包括 LKH 和它的 `timeout` 包装进程。这三个字段位于 `result.json` 的 `adapter` 对象中。原生 LKH 日志中的 `Time` 使用进程用户 CPU 时间。

`TRAVEL_TIME_SECTION` 按含仓库的原始维度逐行列出有向整数时间。适配器默认使用毫秒，对行驶、服务和窗开始向上取整，对窗结束向下取整，仓库班次采用相同规则。时间刻度和成本精度分别记录。时间窗预处理按实际行驶时间识别必迟弧，并沿用上游对被剪弧的处理。其搜索成本记为 `M`，有效时间记为 `max(M, 原行驶时间)`。`M` 是剪弧哨兵；路线的物理行驶时间由公共回放读取。搜索继续以罚值、总距离的顺序比较路线。

上游 LKH-3.0.10 的 `SRC/ReadProblem.c` 第 673–679 行给非对称变换两半中的仓库副本设置相同的非零 `DepotId`。罚函数按物理有向弧将这些副本映射回原仓库。正式补丁在每次读取时间矩阵前检查行、列索引都在 `1..Dim` 内。越界时，`eprintf` 报告映射值、原始维度以及两个端点各自的 `Id` 和 `DepotId`，随后终止求解。

LKH 参数文件指定 `PROBLEM_FILE`、`RUNS=1000000`、`SEED`、`MTSP_MIN_SIZE=0`、`TOTAL_TIME_LIMIT`、`OUTPUT_TOUR_FILE=trial-$.tour` 和 `TOUR_FILE=run-$.tour`。`VEHICLES` 在问题文件中给出。最大试验次数、移动类型、候选集和边成本的 `PRECISION` 沿用原版默认值。`TOTAL_TIME_LIMIT` 取自 `ctx.remaining()`；父进程对完整任务使用墙钟截止。

- LKH-3 在每轮搜索的活动节点循环中无条件查询进程的用户 CPU 时间。CVRP 实测中，系统 CPU 时间约占墙钟时间的 22%–34%。省略 `TOTAL_TIME_LIMIT` 仍会执行这些查询，未能减少这项开销，正式配置保留该参数。
- 正式配置的 `RUNS` 和 `MAX_TRIALS` 未按验证集结果调参。`RUNS=1000000` 使搜索可持续到外层墙钟截止，`MAX_TRIALS` 采用上游默认值，即变换后的内部节点数 `Dimension`。
