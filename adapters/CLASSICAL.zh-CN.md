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
