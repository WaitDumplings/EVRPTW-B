# Chicago 和 Houston 的 Test1 正式评测

本轮用四个 CVRP 求解器评测同一组 300 个 Test1 实例，共 1,200 个“方法 × 实例”任务。每个任务使用一个求解线程和同一个随机种子。三个视图都可以读取同一份 Stage 2 Parquet。[运行环境和依赖准备](../README.md#准备运行环境)

| 城市 | Cus100 | Cus500 | Cus1000 | 合计 |
|---|---:|---:|---:|---:|
| Chicago | 50 | 50 | 50 | 150 |
| Houston | 50 | 50 | 50 | 150 |
| 合计 | 100 | 100 | 100 | 300 |

## 查看计划并运行

从 EVRPTW-B 仓库根目录执行。将 `PY` 指向已准备的控制环境解释器。`DATA_ROOT` 指向包含 `instances/`、`reports/` 的 Stage 2 交付根目录。两城的输入分别位于 `instances/chicago/test1/` 和 `instances/houston/test1/` 下的三个规模目录。为本次配置选择一个可写的结果目录。

```bash
DATA_ROOT=../city-v2-output
OUT=formal/cvrp-test1
args=(--data-root "$DATA_ROOT" --out "$OUT" --view cvrp --solver all
      --cities chicago houston --small-jobs 32 --large-jobs 20
      --seconds 1800 --seed 20261103)

"$PY" scripts/run_test1.py "${args[@]}" --plan-only
"$PY" scripts/run_test1.py "${args[@]}"
```

`--plan-only` 可在启动求解前核对输入和作业计划。上述城市、方法选择、并发数、预算和种子均使用默认值。CVRP 测试依次运行 `ortools`、`hgs`、`pyvrp-ils`、`gurobi`。每个方法分两批处理实例。

| 同一方法的批次 | 输入 | 实例数 | 默认并发上限 |
|---|---|---:|---:|
| 第一批 | 两城 Cus100 与 Cus500 合并 | 200 | 32 |
| 第二批 | 两城 Cus1000 | 100 | 20 |

并发单位是实例。当前方法的第一批完成后才启动第二批，第二批完成后才切换到下一种方法。可按可用物理核和内存调低 `--small-jobs` 和 `--large-jobs`。大实例的模型和矩阵分别占用每个求解进程的内存。

## 检查点和结果

每个任务只启动一次 1,800 秒求解。执行器按事件时间从同一次搜索提取第 60、300、900、1,800 秒的结果，保存到 `checkpoints.json`。各检查点取截至当时已返回、经独立回放可行且总米数最小的路线。若此时还没有可行解，`best` 为 `null`。提前结束的任务在后续检查点沿用此前最好结果。若更改预算，执行器仅保留预算内的标准检查点，并记录所设预算的终点。

每次尝试的 `result.json` 记录输入 ID、SHA-256、种子、预算和终态。求得可行解后，`routes.json` 和 `replay.json` 分别保存预算内的最好路线与逐弧回放。时间预算内无可行解、执行异常、许可问题或回放失败均保留在对应的尝试记录中。统计成功数时以 `status=pass` 为准，失败原因按 `result.json` 的 `status` 和 `reason` 查看。[计时和回放口径](../README.md#计时和回放)

运行期间和结束后均可按方法、视图、城市和规模统计各状态的数量，或导出检查点明细。

```bash
"$PY" scripts/report_test1.py --out "$OUT"
"$PY" scripts/report_test1.py --out "$OUT" --csv "$OUT/checkpoints.csv"
```

CSV 每个实际检查点一行，包含实例 ID、方法、检查点秒数、可行路线总米数等字段。某个任务还在等待或运行失败且没有检查点时，CSV 保留其状态，检查点字段留空。用状态汇总核对任务数，避免把同一任务的四个检查点计为四次求解。

## 中断续跑、补跑与重试

首次正式运行在 `$OUT/manifest.json` 固定城市、Test1 输入清单及各文件的 SHA-256、视图、每例预算和随机种子。中断后用原命令和原 `--out` 续跑，调度器核对清单与已有结果，补齐尚未完成的任务。同一个结果目录同时只允许一个调度器运行。

如果首次只选了部分方法，等当前调度结束后，可以在同一 `--out` 指定另一方法或 `--solver all`，已有完整结果会被跳过。下例沿用本轮的两城输入和默认预算、种子，单独补跑 Gurobi。若改变清单中的城市、输入、视图、预算或种子，请使用新的结果目录。

```bash
"$PY" scripts/run_test1.py --data-root "$DATA_ROOT" --out "$OUT" --view cvrp --solver gurobi
```

解决许可、环境或执行等技术问题后，在原命令末尾加 `--retry-errors`，为对应的技术失败各重试一次。每次重试写入新的尝试目录，历史失败记录保留。预算内无可行解（`no_feasible_within_budget`）属于已完成的测量，续跑与技术重试都会跳过该结果。

```bash
"$PY" scripts/run_test1.py "${args[@]}" --retry-errors
```

## 选择城市、方法和视图

`--cities chicago` 或 `--cities houston` 可单独评测一个城市，每城 150 例。`--solver` 接受 `all` 或一个方法名。`all` 依次运行所选视图的四个方法。

| `--view` | 可选的 `--solver` |
|---|---|
| `cvrp` | `ortools`、`hgs`、`pyvrp-ils`、`gurobi` |
| `vrptw` | `ortools`、`pyvrp-ils`、`pyvrp-hgs`、`gurobi` |
| `evrptw` | `vns-ts`、`routingblocks`、`hma`、`gurobi` |

以下命令可分别预览单个 CVRP 方法、VRPTW 的 PyVRP-HGS 和 EVRPTW 的 RoutingBlocks 作业计划。需要执行相应评测时移除 `--plan-only`，并使该方法的依赖和许可就绪。

```bash
"$PY" scripts/run_test1.py --data-root "$DATA_ROOT" --out formal/cvrp-test1-chicago --cities chicago --view cvrp --solver hgs --plan-only
"$PY" scripts/run_test1.py --data-root "$DATA_ROOT" --out formal/vrptw-test1 --view vrptw --solver pyvrp-hgs --plan-only
"$PY" scripts/run_test1.py --data-root "$DATA_ROOT" --out formal/evrptw-test1 --view evrptw --solver routingblocks --plan-only
```

如仅需诊断指定的 Parquet，可将文件直接交给底层 `bench.py run` 或 `bench.py batch`。两城完整 Test1 的分批调度和续跑由 `scripts/run_test1.py` 负责。[底层运行示例](../README.md#单例复跑)

### 接入表外的方法

新增方法时在 `runtime.py` 的 `SOLVERS` 末尾登记方法名、模块和视图，以保留已有结果目录的编号。适配器实现 `solve(method, task, ctx)`，在 `ctx.remaining()` 给出的剩余预算内搜索，用 `ctx.emit(routes)` 即时返回完整路线。路线采用原始 `node_idx`，每辆车从 0 出发并返回 0。EVRPTW 适配器还需实现同实例的 `charge_probe`。随后用同一份 Test1 清单、种子、截止时间和回放器执行 `--solver 新方法名`。独立环境可通过 `EVB_PYTHON_方法名` 指定，变量中的方法名使用大写，连字符改为下划线。
