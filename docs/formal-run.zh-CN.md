# 正式评测调度

`scripts/run_formal.py` 从 EVRPTW-D 公开版的 `index.parquet` 选择实例。每项预算 1,800 秒，单线程，默认种子 20261103。每份实例默认有 11 个启发式方法与视图组合。使用 `--gurobi` 时，Cus100 另外运行 Gurobi 的三个视图。[方法注册表](../runtime.py)

## 先看任务计划

从仓库根目录执行。将 `PY` 设为安装了 PyArrow 的基准控制环境解释器，`DATA_ROOT` 设为公开数据根目录。下例只读取目录和索引，在标准输出列出每个任务的城市、划分、客户数、实例 ID、方法和视图。首行给出实例数、任务数及单核搜索核时。输入大小按索引核对，文件的 SHA-256 在实际运行该任务时再由 `bench.py` 对照索引验证。若发布目录有 `release.json`，调度器还会核对其索引哈希。

```bash
"$PY" scripts/run_formal.py --data-root "$DATA_ROOT" --plan-only > plan.tsv
```

| 默认公开测试范围 | 实例数 | 启发式任务 | 单核搜索核时 |
|---|---:|---:|---:|
| test1 | 1,500 | 16,500 | 8,250 |
| xs | 500 | 5,500 | 2,750 |
| test2 | 1,500 | 16,500 | 8,250 |
| 全部 | 3,500 | 38,500 | 19,250 |

全范围启用 Gurobi 后共 41,500 项、20,750 核时。建模、独立回放以及每例结束后的初始解重现另行耗时。

`--cities` 和 `--splits` 各接受一个或多个名称，默认覆盖所有城市和上述三个测试划分。调度先处理 test1 和 xs，再处理 test2。每一部分按客户数 100、500、1000、2000 分组，等上一组结束后再启动下一组。每组按实例 ID 字典序派发，不同核心上的任务可在不同时刻完成。

`--test2-range FIRST LAST` 选择每个 test2 规模的相同序号段，按所选城市中该规模的实例 ID 字典序编号，起止均包含，首项为 1。也可重复指定 `--test2-range SIZE FIRST LAST`。仅给出分规模区间时，只选择列出的 test2 规模。两种形式并用时，分规模区间覆盖该规模的通用区间。下面两条命令分别选择 6 份和 4 份 test2 实例。

```bash
"$PY" scripts/run_formal.py --data-root "$DATA_ROOT" --splits test2 \
  --cities jacksonville --test2-range 1 2 --plan-only
"$PY" scripts/run_formal.py --data-root "$DATA_ROOT" --splits test2 \
  --test2-range 100 11 12 --test2-range 1000 21 22 --plan-only
```

## 运行与续跑

实际执行时提供 `--out`。使用 `--reader` 或 `EVRPTWD_READER` 指定能读取公开 v2 格式的 EVRPTW-D 仓库。未指定时程序探测当前默认读取器及同级 `EVRPTW-D-HF-two-part` 检出目录，并用一份所选文件试读。非学习求解器的安装、环境和原生程序构建见[仓库说明](../README.md)。

```bash
"$PY" scripts/run_formal.py --data-root "$DATA_ROOT" --reader "$READER_V2" \
  --out "$OUT" --max-jobs 16
```

每个并发位置使用一个物理核，包含该实例的全部求解任务以及随后的构造记录进程。默认最多使用进程可用物理核的一半，`--max-jobs` 可设置实际并发上限。同一用户的正式调度器在本机运行时通过本地文件锁预留插槽与物理核编号，因此使用同核不同 SMT 线程的两个进程也会互斥。若部分核心已被另一份计划预留，调度器使用可用位置。

`OUT/manifest.json` 固定上次选择的输入、索引哈希、方法范围、预算与种子。相同计划可在同一目录续跑，并可调整并发数。更改测试范围、Gurobi 设置或种子时使用另一输出目录。调度器继承已有的 `bench.py` 结果格式，每项存放在 `OUT/<实例ID>/<编号>-<方法>-<视图>/attempt-NNN/`。终态 JSON 经临时文件改名发布，成功任务还须具备其路线、回放和规定的全部检查点。未完成的尝试保留以供诊断，续跑另建尝试。技术错误默认保留，可用 `--retry-errors` 重新运行一次。预算内无可行解是完整的测量结果。

同一实例的计划内任务全部结束后，调度器才在保留给该实例的物理核上启动独立进程，依次收集 OR-Tools 的 CVRP、VRPTW 及 VNS/TS、RoutingBlocks 的 EVRPTW 初始解，各写入所属尝试的 `initial_solution.json`。若中断发生在构造收集期间，续跑只补未完成的构造记录。用下列命令按方法、视图、划分与规模汇总。

```bash
"$PY" scripts/report_initial.py --out "$OUT" > initial-summary.csv
```
