# 初始解记录

每个实例的全部计时任务结束后，正式调度器把仍由该实例独占的物理核交给构造进程。同一实例的多个构造进程依次运行。每条记录放在相应正式任务的 `attempt-NNN/initial_solution.json`。方法预算、检查点和最终成绩仍取自同一场正式搜索。

| 方法 | 视图 | 记录的原始起点 | construction_s 的计时区间 |
|---|---|---|---|
| OR-Tools GLS | CVRP、VRPTW | 使用同一模型、并行最廉价插入策略和种子，设置 `solution_limit=1`，在首个赋值停止 | 只求首解的 `SolveWithParameters` 调用 |
| VNS/TS | EVRPTW | `--construct-only` 调用第一次 `initial()`，保留进入 VNS 前的有序路线 | 原生 `initial()` 调用 |
| RoutingBlocks | EVRPTW | 路网适配器第一次 `random_construction` 交给第一次局部搜索的路线；后续起点之间已发生局部搜索 | 进入 `WeightedALNS.run` 到首次构造回调 |

三个时间均在事后进程测得。输入读取、单独进程启动、独立回放和结果序列化在这些区间之外。VNS/TS 的构造模式须由本次源码编译。可用 `"$PY" adapters/prepare_electric.py --vns-only` 更新对应环境中的可执行文件。

## 文件字段

`instance_id`、`sha256`、`city`、`split`、`n`、`view`、`method`、`seed`、`seconds` 标识原任务及输入。取得方式 `acquisition` 为 `posthoc_reproduction`。`routes` 保留车辆与节点的原始顺序，含每条路线的起终仓库 0，省略未使用的车辆槽。`vehicles` 为实际路线数，`charging_visits` 按到站次数计，同一站可多次计数。

- `status=recorded` 表示取得原始路线，`distance_m` 是逐弧按原始有向浮点矩阵求和的米数，包含不可行路线。`feasible` 使用基准的独立回放器判定。`timed_handoff` 表明正式档案能否确认原运行进入了搜索；`unconfirmed` 表示正式日志缺少交接证据。
- `status=no_initial_solution` 表示构造未产出解，或者原计时任务有明确证据表明构造未取得起点。路线及目标值保留为空。`reason` 给出依据；确认没有启动构造时，其耗时也为空。若 OR-Tools 正常运行但首解回调未交付路线，另行重跑首解策略计时；事后若产生路线，只列在 `reproduction` 中。
- 若现有档案无法确认正式搜索曾取得起点（如 RoutingBlocks 在首轮初始化阶段到期，尚未提交可行候选），标记 `unknown`。事后实际重现的路线和回放量保存在 `reproduction` 中，汇总单列这些任务。
- `violations` 列出所有非零违反项，`replay_error` 保留公共回放器遇到的第一条错误。客户覆盖分别统计未服务客户数、重复服务次数。载量取各路线超载之和（cm³）。时间窗分别累计客户服务开始迟到秒数、回仓超班秒数，并报告违反次数。电量以每次到达时相对保留电量的缺口（kWh）统计总和、最大值及次数；连续低电量的多次到达分别计数。对违规路线仍顺序推进时钟和电量，按原始路线的到站顺序充满和等待。
- 可行性使用公共回放的数值容差，载量为 `1e-6` cm³、时间为 `1e-8` 秒、电量为 `1e-8` kWh。

## 汇总

```bash
"$PY" scripts/report_initial.py --out "$OUT" > initial-summary.csv
```

每行对应方法、视图、split、规模的一组。任务总数来自正式调度清单。`feasible_rate` 的分子为已记录且通过回放的初始解数，分母包括该组全部任务。`pending`、`unknown`、`no_initial_solution` 分列，让正在运行或未能确认的任务在分母中可见。

`mean_initial_m` 对实际记录的初始距离取算术平均，包括不可行初解。对有初始距离且最终正式结果通过回放的配对任务，`mean_improvement_m` 取「初始距离减最终距离」的平均值，`mean_improvement_pct` 取每任务相对其初始距离的降幅再平均；初始距离为零的配对只计入米数。不可行的短路线经搜索变为可行路线后可能更长，对应降幅可以为负数。CSV 给出参与平均的任务数。
