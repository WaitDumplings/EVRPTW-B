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

所有方法把客户数作为可用车辆上限，并保留零固定成本。OR-Tools 和两版 PyVRP 把每个方向的距离就近换算为毫米，时间用毫秒，对行驶、服务、窗开始向上取整，对窗结束向下取整。容量和需求使用每 cm³ 1,000 个整数单位，需求向上、容量向下取整。VRPTW 在客户的服务开始时刻施加硬窗，班次同时约束离仓与回仓。最终距离与可行性由公共回放器使用原始浮点字段计算。

HGS 通过作者的 `solve_cvrp_dist_mtx` 接收按行存放的有向 double 矩阵，用 km 和 L 表示距离和体积。坐标供原搜索使用。构建脚本为作者首次及历次全局可行最优的赋值加上路线通知，并将其循环时限接到单调墙钟。OR-Tools 在 Routing Solver 的解事件中提取路线，PyVRP 0.14 通过 ILS 的初始解和最优解回调提取路线，0.9 观察原 GA 的最优解赋值。两版各自在原搜索入口运行。

源节点 0 是 depot，客户依 Parquet 顺序注册。OR-Tools 用索引管理器还原节点，PyVRP 0.14 通过每个客户 activity 对应的 location 还原，0.9 和 HGS 的客户编号与源索引相同。每条路线都补全起终 depot，并在搜索进行中写入公共事件流。
