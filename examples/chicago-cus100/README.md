# Chicago Cus100 单例运行记录

所有记录使用 Chicago 训练集的同一个 Cus100 实例。12 个组合的单次预算均为 60 秒。表中最好路线均已独立回放。每项使用 1 个物理核、1 个求解线程和搜索 seed `20261103`。

实例 ID 为 `626c1a86d16d7c06ab213f62`，Parquet 的 SHA-256 为 `7b9061e7ff08e1de2567c24a6b41d10a28ea6adb2c582418b09fd3ceb4aa4b91`。路线使用原始 `node_idx`，距离按原始有向最短路矩阵以米计。

验收数据由 EVRPTW-D 生成。路网与地图设施来自 © OpenStreetMap contributors，经 Geofabrik 提供；订单模型使用 Amazon Last Mile Routing Research Challenge 数据（CC BY-NC 4.0）。[固定版本的数据来源与署名](https://github.com/WaitDumplings/EVRPTW-D-HF/blob/e2cf4825dd6a7c4ecd2cf88bef6cb206908ae50e/NOTICE.md)

表中时间从适配器收到实例开始计算。[完整精度 CSV](summary.csv) 包含版本、首个可行解和最好解的时间、独立回放耗时，以及精确模型的界和终态。

| 求解器 | 视图 | 首解秒 | 墙钟秒 | 回放距离 m | 车数 | 访站 | 记录 |
|---|---|---:|---:|---:|---:|---:|---|
| OR-Tools GLS | CVRP | 0.065 | 59.221 | 42,764.690 | 1 | — | [路线](01-ortools-cvrp/routes.json) · [回放](01-ortools-cvrp/replay.json) |
| OR-Tools GLS | VRPTW | 0.072 | 59.220 | 42,764.690 | 1 | — | [路线](02-ortools-vrptw/routes.json) · [回放](02-ortools-vrptw/replay.json) |
| HGS-CVRP | CVRP | 0.010 | 59.300 | 42,224.139 | 1 | — | [路线](03-hgs-cvrp/routes.json) · [回放](03-hgs-cvrp/replay.json) |
| PyVRP-ILS | CVRP | 0.052 | 59.301 | 42,224.139 | 1 | — | [路线](04-pyvrp-ils-cvrp/routes.json) · [回放](04-pyvrp-ils-cvrp/replay.json) |
| PyVRP-ILS | VRPTW | 0.053 | 59.301 | 42,224.139 | 1 | — | [路线](05-pyvrp-ils-vrptw/routes.json) · [回放](05-pyvrp-ils-vrptw/replay.json) |
| PyVRP-HGS | VRPTW | 0.049 | 59.301 | 42,797.887 | 1 | — | [路线](06-pyvrp-hgs-vrptw/routes.json) · [回放](06-pyvrp-hgs-vrptw/replay.json) |
| VNS/TS（C++） | EVRPTW | 0.043 | 59.307 | 42,888.685 | 1 | 0 | [路线](07-vns-ts-evrptw/routes.json) · [回放](07-vns-ts-evrptw/replay.json) · [充电探针](07-vns-ts-evrptw/station_probe.json) |
| RoutingBlocks ALNS | EVRPTW | 0.171 | 59.300 | 122,405.202 | 4 | 0 | [路线](08-routingblocks-evrptw/routes.json) · [回放](08-routingblocks-evrptw/replay.json) · [充电探针](08-routingblocks-evrptw/station_probe.json) |
| HMA | EVRPTW | 0.194 | 48.507 | 42,538.635 | 1 | 0 | [路线](09-hma-evrptw/routes.json) · [回放](09-hma-evrptw/replay.json) · [充电探针](09-hma-evrptw/station_probe.json) |
| Gurobi MILP | CVRP | 0.309 | 58.310 | 42,382.151 | 1 | — | [路线](10-gurobi-cvrp/routes.json) · [回放](10-gurobi-cvrp/replay.json) |
| Gurobi MILP | VRPTW | 0.498 | 58.317 | 43,114.660 | 1 | — | [路线](11-gurobi-vrptw/routes.json) · [回放](11-gurobi-vrptw/replay.json) |
| Gurobi MILP | EVRPTW | 1.088 | 58.335 | 43,420.220 | 1 | 0 | [路线](12-gurobi-evrptw/routes.json) · [回放](12-gurobi-evrptw/replay.json) · [充电探针](12-gurobi-evrptw/station_probe.json) |

C++ VNS/TS 返回的 11 条候选均通过独立回放。求解参数为 `--granular 30 --init-time-check 0`。最好路线在第 20.716 秒取得。此次构建的源码 SHA-256 为 `980fa2ddc46a9e21b05766fd32e6b4de71bc43f678d951eeb570e248cae8976c`。

四组 EVRPTW 路线均为零访站。各组探针另外选取本例中的真实有向弧和充电站，核对弧耗电、按站点有效功率充满所需的时间，以及离站电量。

Gurobi 的三个视图均以 `TIME_LIMIT` 结束。CVRP、VRPTW 的界对应完整模型。EVRPTW 模型中每座物理站的两个副本由全车队共用，其下界对应这个受限模型，已回放的路线给出原实例的可行上界。
