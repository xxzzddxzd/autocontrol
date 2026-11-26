# autocontrol

本地自动执行总控台：一个常驻的总控进程按登记表调度所有项目的执行器子进程，把每一轮的进度实时写到一张飞书卡片上，归档结果并记录运行指标。不需要给项目写 crontab。Python ≥ 3.10，无第三方依赖。

![卡片示意（示例数据）](docs/card-preview.png)

## 概念

- **项目**：一个被调度的执行器，两种接入方式：
  - 协议项目：执行器按协议 v1 输出，用清单 `autocontrol.json` 描述，命令行写 `--manifest <清单>`。
  - 内置适配器：`autocontrol/projects/<名称>/` 适配的存量执行器，命令行写 `--project <名称>`。
- **私有目录**（`--private`；协议项目默认取清单的 `private` 字段）：锁、结果、指标、卡片绑定和飞书配置都在这里，不进仓库。
- **总控进程**（`console`）：整台机器只跑一个。按登记表 `~/.config/autocontrol/console/projects.json` 给每个启用的项目各起一个调度进程（`serve`）。起的时候一般带 `--observe-first`，先只读读取一次；只是总控代码换了的，带 `--resume`，不登录，沿用已保存的卡片，等下一轮。调度进程退出后，总控隔一段时间把它重启，间隔从 30 秒逐次翻倍，最长 10 分钟。项目被停用、移除或改了设置时，总控等它这一轮跑完再停。调度进程只在启动时读一次清单和总控代码：清单改动重新通过准入后，总控在两轮之间自动重启它，加载新清单；总控代码换了（文件一分钟内不再变化，且在新进程里能正常导入）后，也是各项目在自己的两轮之间各重启一次，总控进程自己则原地换成新代码（pid 不变），把正在跑的调度进程接回来。执行器每个阶段都是新进程，改执行器代码不用重启。项目上线就是登记（`register`），下线就是移除（`unregister`）。
- **服务日**：从清单 `schedule.reset` 起的 24 小时。重置早于 12:00 的按开始日期命名，12:00 及以后的按次日命名。
- **一轮**：按清单 `phases` 的顺序，每个阶段运行一次执行器。唤醒时刻包括清单的固定时刻 `schedule.times`，以及执行器在结果里上报的 `next_wake`。换日时刻只做只读发现；如果 90 分钟内就有一轮，换日时不登录，按清单直接出新一天的卡片，由那一轮读取服务器。阶段可以有自己的时刻表（`schedule.phases`，见接入规范「多套调度」），卡片上排在「循环任务」区。
- **卡片**：每个项目每个服务日一张，执行中持续更新同一张。已完成的项写当天第一次完成的时间，重试后完成、核对后确认、手动补跑完成和今日已到上限会加注；同一任务连续失败时写明连续几轮。

## 命令

在仓库根目录运行：`python3 -B run.py <命令> (--manifest <清单> | --project <名称>) [--workdir <目录>] [--private <目录>]`。`console`、`unregister`、`projects` 不需要项目参数。

**必须显式写 `--manifest` 或 `--project`**：两个都不写时，默认用内置适配器跑真实执行器。

| 命令 | 作用 |
|---|---|
| `console` | 总控进程：按登记表调度所有项目。已经在运行时直接退出 |
| `autostart [--disable]` | 配置（或取消）一个 launchd 代理 `local.autocontrol.console`：登录时启动总控，总控崩溃时自动拉起。只此一条，和项目数量无关 |
| `register [--disable]` | 登记项目，交给总控调度（上线）。协议项目要先通过准入；私有目录里没有飞书配置时，这个项目只在看板上显示。`--disable` 登记但暂停调度。总控 30 秒内生效 |
| `unregister <名称>` | 从登记表移除（下线）。它的调度进程跑完当前这一轮后停止 |
| `projects [--json]` | 列出登记的项目，以及总控和各项目调度进程的状态 |
| `check [--static] [--repeat N] [--json]` | 准入检查。协议项目通过后，`register`、`serve`、`once` 才会运行；清单改动后要重新检查，通过后总控在两轮之间自动加载新清单。`--static` 不运行执行器 |
| `once --observe-only` | 只读发现当日计划并更新卡片，不发起任何变更 |
| `once [--phase <阶段>]` | 立即执行一整轮，或只执行一个阶段 |
| `serve [--observe-first \| --resume] [--reason R]` | 单个项目的常驻调度，平时由总控启动。每个项目只能有一个，由 `coordinator.lock` 保证。`--reason` 是启动原因，只写进日志 |
| `metrics [--days N] [--json]` | 运行指标与健康结论 |
| `preview [--snapshot <快照>] [--demo running\|finished\|done] [--out <卡片.json>] [--html <预览.html>]` | 本地渲染卡片，不联网。协议项目必须给 `--snapshot` |
| `chats` | 列出机器人所在的群，用来查 `chat_id` |
| `schema [--out docs/schemas]` | 导出协议 JSON Schema |
| `dashboard [setup \| status \| allow <open_id> \| deny <open_id> \| webhook [test\|off]]` | 网页看板。不带参数时运行看板服务，平时由总控托管；`setup --url <https 地址> --app <lark_app.json>` 写设置，`allow`/`deny` 管白名单，`webhook` 配置申请提醒用的群机器人（交互输入，不回显）；`--dev` 本机预览，不登录 |
| `cards on\|off\|status` | 飞书卡片总开关。`off` 时每轮的快照只存本地给看板用，不调飞书接口；`on` 恢复发送，也会解除额度暂停。`status` 显示是否因本月额度用完而暂停。各调度进程几秒内生效 |

`--skip-admission` 只用于调试。

## 接入新脚本

脚本作者只按 [docs/接入规范.md](docs/接入规范.md) 改造；下面是维护者上线的步骤。

1. 按 [docs/接入规范.md](docs/接入规范.md) 改造脚本并写 `autocontrol.json`。Python 脚本把 `sdk/python/autocontrol_worker.py` 复制到脚本旁边使用。完整示例见 [examples/demo_worker/](examples/demo_worker/)。
2. 要发飞书卡片时，在私有目录放 `lark_app.json`：权限 0600，字段为 `app_id`、`app_secret`、`chat_id`。卡片发到哪个群要和用户确认，`chats` 列出机器人所在的群。不放也能登记，这个项目只在看板上显示。
3. `check --manifest <清单> --repeat 2`，所有必须项都要通过。
4. `once --manifest <清单> --observe-only`，在看板上确认这个项目的清单和状态。
5. `register --manifest <清单>`，总控会在 30 秒内接管，先跑一轮只读；`projects` 查看状态。
6. 撤掉脚本原来的调度（crontab 行、常驻进程、launchd 任务）和它自带的通知，免得两边都跑、出现两份消息。
7. 第一次真实执行后，核对看板上的状态和结果。暂停用 `register --disable`，下线用 `unregister <名称>`，都会等当前这一轮跑完。

## 网页看板

看板可以代替机器人卡片：一页列出所有项目今天的进度，点进去就是和卡片一样的清单，下面附最近几轮的记录和调度日志。按手机屏幕设计，支持深色模式，每 30 秒自动刷新，可以翻看前几天。

- **不占飞书额度**：页面只读本机文件（登记表、总控状态、各项目保存的卡片快照、指标库、调度日志），每张卡片都用项目自己的卡片代码渲染，内容和飞书卡片一致。登录只用飞书的「认证及授权」接口（授权页、换 user_access_token、读用户信息），按飞书规则不计入企业每月的 API 调用量。
- **登录**：飞书网页授权（授权码 + PKCE）。在飞书客户端里打开时可以免确认。只有管理员和白名单里的人能看；还没有管理员时，`setup` 之后 24 小时内第一个登录的人成为管理员。会话 Cookie 有效 14 天，改了设置里的 `secret` 所有人都要重新登录。
- **申请与审批**：不在白名单的人登录后会记成一条申请（`board_people.json`，0600），同一个人一天最多提醒一次。配置了群机器人（`dashboard webhook`）时，管理员在飞书群里收到带「批准」「拒绝」按钮的卡片；自定义机器人的 webhook 消息不计入飞书 API 调用量。卡片按钮只打开看板的审批页，在页面上再点一下才生效（表单带和会话绑定的校验值）。看板的「成员」页列出待批准的申请和白名单，也能移出某个人；只有管理员能进。拒绝后七天内不再提醒。
- **和飞书卡片的关系**：看板不依赖飞书。飞书返回「本月 API 调用次数已达上限」（99991403）时，所有项目的卡片一起暂停到下月 1 日 00:05（北京时间）自动恢复，暂停记在 `lark_quota.json`，期间快照照常存给看板；`cards on` 可以提前解除（例如升级了飞书版本）。没有飞书配置或配置有误的项目，快照也照常存给看板。
- **运行**：总控按 `~/.config/autocontrol/console/dashboard.json`（0600）托管看板进程，只监听 127.0.0.1；退出后由总控拉起，换了总控代码时随总控一起换新代码。设置里写了 `tunnel`（cloudflared 配置文件）时，总控也托管隧道进程，公网经 HTTPS 访问。
- **接入步骤**：
  1. 准备一个固定的 HTTPS 地址（例如 Cloudflare 隧道），转发到本机看板端口（默认 8790）。
  2. `dashboard setup --url https://<地址> --app <某个私有目录>/lark_app.json`。
  3. 在飞书开发者后台给这个应用加「网页应用」能力，主页填看板地址；在「安全设置」里把 `https://<地址>/auth/callback` 加为重定向 URL；可用范围只留需要看看板的人；创建版本并发布。
  4. 打开看板用飞书登录，第一个登录的人成为管理员。确认能看到之后，`cards off` 停发机器人卡片。

## 运维规则

- **启动总控**：配置了 `autostart` 时由 launchd 在登录时启动，崩溃后自动拉起；没配置时手动运行 `python3 -B run.py console`。
- **停止总控**：`kill -TERM $(lsof -t ~/.config/autocontrol/console/console.lock)`。空闲的调度进程马上退出，正在跑的那一轮会先跑完；总控不会把一轮截断。用 SIGTERM 停掉的总控 launchd 不会拉起，要再启动时运行 `launchctl kickstart gui/$(id -u)/local.autocontrol.console`。换了总控代码不用手动重启，见上文。
- **单独重启某个项目**：停掉它的调度进程（`kill -TERM $(lsof -t <私有目录>/coordinator.lock)`），总控会在 30 秒后带 `--observe-first` 把它重新拉起。只能在两轮之间操作，即私有目录的 `round.lock` 没有被占用时。锁的持有者不是总控启动的进程时，先确认再停。
- **手动起的 `serve`**：总控发现某个项目已经有调度进程在跑（不是它起的）时，不会再起第二个，等那个进程退出后再接管。
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
| `logs/` | 每个阶段执行器 stderr 的最后 16 KiB，保留 7 天，只供排查 |
| `metrics.sqlite3` | 阶段、轮次、任务日终状态（含卡片口径的完成判断和连续失败轮次） |
| `daily_board.sqlite3` | 每个服务日的卡片绑定与最新快照 |
| `lark_assets.json` | 机器人已上传的卡片图片（执行中的转圈图标）缓存 |
| `admission/` | 最近一次准入检查记录 |

总控自己的文件在 `~/.config/autocontrol/console/`（0700）：登记表 `projects.json`、锁 `console.lock`、状态 `status.json`、换代码时的交接文件 `handover.json`（新代码启动时读完即删），看板设置 `dashboard.json`、看板申请记录 `board_people.json`、卡片开关 `cards.json`、额度暂停 `lark_quota.json`，以及日志：`logs/console.log` 记总控自己的决定（为什么启动、停止、重启哪个项目，什么时候换代码）；`logs/<名称>.log` 是各项目的调度日志，每行一个决定：启动原因、每一轮为什么开（固定时刻、服务器时刻、阶段时刻、换日、失败后重读）、结果（状态、退出码、卡片送达、用时）和下一轮的时刻，也收调度进程的报错；`logs/dashboard.log` 是看板的访问和登录记录（只记路径，不记查询参数）。

## 开发

- 测试：`python3 -m pytest`。不联网，不运行真实执行器。
- 格式：black 和 ruff，行宽 100，见 `pyproject.toml`。
- 内置适配器的结果解析是从上游执行器移植的副本（见各模块文档字符串），上游结果格式变化时需要同步。

```
autocontrol/   console（总控进程）、registry（登记表）、schedule、runner、service、protocol、admission、metrics；board/ 卡片；lark/ 飞书；dashboard/ 网页看板；projects/ 项目接入
sdk/python/    脚本侧 SDK
examples/      协议接入示例
docs/          接入规范、JSON Schema
tests/
```
