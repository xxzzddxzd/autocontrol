# autocontrol

本地自动执行总控台：按计划调度各项目的执行器子进程，把每一轮的进度实时写到一张飞书卡片上，归档结果并记录运行指标。Python ≥ 3.10，无第三方依赖。

![卡片示意（示例数据）](docs/card-preview.png)

## 概念

- **项目**：一个被调度的执行器，两种接入方式：
  - 协议项目：执行器按协议 v1 输出，用清单 `autocontrol.json` 描述，命令行写 `--manifest <清单>`。
  - 内置适配器：`autocontrol/projects/<名称>/` 适配的存量执行器，命令行写 `--project <名称>`。
- **私有目录**（`--private`；协议项目默认取清单的 `private` 字段）：锁、结果、指标、卡片绑定和飞书配置都在这里，不进仓库。
- **服务日**：从清单 `schedule.reset` 起的 24 小时。重置早于 12:00 的按开始日期命名，12:00 及以后的按次日命名。
- **一轮**：按清单 `phases` 的顺序，每个阶段运行一次执行器。唤醒时刻包括清单的固定时刻 `schedule.times`，以及执行器在结果里上报的 `next_wake`。换日时刻只做只读发现。
- **卡片**：每个项目每个服务日一张，执行中持续更新同一张。

## 命令

在仓库根目录运行：`python3 -B run.py <命令> (--manifest <清单> | --project <名称>) [--workdir <目录>] [--private <目录>]`。

**必须显式写 `--manifest` 或 `--project`**：两个都不写时，默认用内置适配器跑真实执行器。

| 命令 | 作用 |
|---|---|
| `check [--static] [--repeat N] [--json]` | 准入检查。协议项目通过后，`serve`、`once`、`install` 才会运行；清单改动后要重新检查。`--static` 不运行执行器 |
| `once --observe-only` | 只读发现当日计划并更新卡片，不发起任何变更 |
| `once [--phase <阶段>]` | 立即执行一整轮，或只执行一个阶段 |
| `serve [--observe-first]` | 常驻调度。每个项目只能有一个，由 `coordinator.lock` 保证 |
| `install [--no-start]` | 在 crontab 写入 `# BEGIN AUTOCONTROL <名称>` 段，每 5 分钟检查并拉起 `serve`；其他 crontab 行不变 |
| `metrics [--days N] [--json]` | 运行指标与健康结论 |
| `preview [--snapshot <快照>] [--demo running\|finished\|done] [--out <卡片.json>] [--html <预览.html>]` | 本地渲染卡片，不联网。协议项目必须给 `--snapshot` |
| `chats` | 列出机器人所在的群，用来查 `chat_id` |
| `schema [--out docs/schemas]` | 导出协议 JSON Schema |

`--skip-admission` 只用于调试。

## 接入新脚本

1. 按 [docs/接入规范.md](docs/接入规范.md) 改造脚本并写 `autocontrol.json`。Python 脚本把 `sdk/python/autocontrol_worker.py` 复制到脚本旁边使用。完整示例见 [examples/demo_worker/](examples/demo_worker/)。
2. 在私有目录放 `lark_app.json`：权限 0600，字段为 `app_id`、`app_secret`、`chat_id`。
3. `check --manifest <清单> --repeat 2`，所有必须项都要通过。
4. `once --manifest <清单> --observe-only`，确认卡片内容。
5. `install --manifest <清单>`。

## 运维规则

- **重启 `serve`**：只能在两轮之间操作，即私有目录的 `round.lock` 没有被占用时；新进程必须带 `--observe-first`，不带时启动后会立即补跑一整轮。cron 看护拉起的进程不带这个参数。
- **停止服务**：`kill -TERM $(lsof -t <私有目录>/coordinator.lock)`，等锁释放后再启动新进程。锁的持有者不是自己启动的进程时，先确认再停。
- **账号锁**：执行器的账号锁（清单 `account_lock`）被占用时，到期的一轮每分钟重试一次，最长 30 分钟。
- **运维开关**：私有目录的 `autocontrol.json` 存放运维开关，可选。例如适配器支持时写 `{"shared_session": true}`，一轮只起一个执行器进程。从下一轮起生效，不需要重启。
- **隐私**：仓库是公开的。不要输出或提交 `lark_app.json` 的内容、`chat_id`、`message_id` 或任何账号信息。

## 私有目录

| 路径 | 内容 |
|---|---|
| `lark_app.json` | 飞书机器人配置 |
| `autocontrol.json` | 运维开关（可选） |
| `coordinator.lock`、`round.lock` | 常驻服务锁、正在执行一轮 |
| `results/` | 每个阶段一份固定字段的结果 JSON |
| `metrics.sqlite3` | 阶段、轮次、任务日终状态 |
| `daily_board.sqlite3` | 每个服务日的卡片绑定与最新快照 |
| `admission/` | 最近一次准入检查记录 |

## 开发

- 测试：`python3 -m pytest`。不联网，不运行真实执行器。
- 格式：black 和 ruff，行宽 100，见 `pyproject.toml`。
- 内置适配器的结果解析是从上游执行器移植的副本（见各模块文档字符串），上游结果格式变化时需要同步。

```
autocontrol/   schedule、runner、service、protocol、admission、metrics；board/ 卡片；lark/ 飞书；projects/ 项目接入
sdk/python/    脚本侧 SDK
examples/      协议接入示例
docs/          接入规范、JSON Schema
tests/
```
