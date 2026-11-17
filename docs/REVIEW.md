# 原 `yslzm/tools` 自动执行总控台 Review

范围：`tools/daily_board/`（model、render、controller、service、stream、store、publisher、resilient、feishu）、`tools/run_scheduled.py`、`tools/result_diagnostics.py`、`tools/notify_daily_result.py`、`tools/install_local_daily.py`、`tools/local_daily.py`，以及执行器侧的 `business/progress_events.py`。基线：原有 144 个相关测试全部通过。

## 值得保留的设计

- 展示与执行严格分离：卡片、SQLite、结果文件都不参与业务判断，每次运行重新读取服务器。
- 本轮「已发出未得到结果」的请求集合只在内存中跨阶段传递，防止重放。
- 05:00 服务日归属、只读发现、跨日任务归属开始的服务日。
- 进度事件、结果摘要、存储全部走白名单，敏感信息不落盘。
- 通知失败不阻断业务；发卡使用稳定 UUID + 历史找回 + 窗口内重放 + 补 PATCH，避免重复卡片。

这些语义在 autocontrol 中全部保留，并有对应测试。

## 发现的问题与处理

| # | 级别 | 问题 | autocontrol 中的处理 |
|---|---|---|---|
| 1 | 高 | **本轮失败被阶段汇总覆盖**：执行中上报的 `task_failed`（如宠物护理异常）在阶段结束时被 `full_checklist_summary` 的 `incomplete` 覆盖，最终卡片上失败原因消失，与「真实失败保留在主列表」的要求相反 | 本轮直接观测到的失败保留到服务器确认完成为止（`test_failure_seen_this_round_survives…`、端到端测试覆盖） |
| 2 | 高 | **成功状态被显示为「状态未返回」**：执行器发出的 `already_confirmed`、`current_state_reconciled`、`current_goal_reconciled`、`read_confirmed`、`receipt_and_attachment_bit_cleared`、`salary_collection_complete`、`no_current_topic`、`no_coin_reward`、`no_reward` 等（其中多个在 `progress_events.SUCCESS` 里）在看板中都落入默认的 `result_unavailable`；`no_current_offer`、`score_insufficient`、`not_ready`、`no_dispatchable_tasks`、`prepared` 同样被误归类 | 按语义映射到完成 / 关闭 / 阻塞 / 等待 / 部分完成；交叉检查确认执行器全部任务 ID、别名、原因码、单位都能被识别 |
| 3 | 中 | **固定计划缺少制作阶段**：制作没有聚合目标，「先发布固定 todo 清单」时金币/体力购买、心意/主线制作四项缺席，直到制作阶段运行才出现 | 制作四项纳入固定计划 |
| 4 | 中 | **单阶段异常中断整轮**：`run_scheduled` 抛出非 `OSError` 异常时后续阶段被跳过，`finish_run` 不执行，卡片停在「执行中」 | 每阶段独立异常边界，`finally` 中收尾并发布 |
| 5 | 中 | **只读发现失败重试过激**：失败后固定 60 秒重试，每次都会真实登录；服务器长时间不可用时约每小时 60 次登录 | 指数退避 60s → 120s → … → 15min，成功后复位 |
| 6 | 中 | **通知配置缺失拖慢执行**：`ResilientPublisher` 没有可用发布器时队列不清空，开局 `flush(30)` 空等满 30 秒才启动执行器 | 配置缺失立即失败返回，执行器不被拖慢；配置恢复后自动补发最新快照 |
| 7 | 中 | **双层发布线程**：`ResilientPublisher` 包裹 `BoardPublisher`，两套队列、两个线程、每秒互相轮询状态，约 500 行，竞态难以推理 | 合并为单线程 `CardPublisher`：每天只保留最新快照，配置变更即时生效，失败指数退避，幂等与恢复语义不变（原发布测试全部移植通过） |
| 8 | 中 | **词表多处重复且已分歧**：任务标题/状态/原因在 `model.py`、`notify_daily_result.py`、`run_scheduled.py`、`progress_events.py` 各一份，标题已不一致（`penglai` 画中蓬莱/蓬莱，`discount_package_reward` 降价奖励/叠叠乐奖励，`atlas_daily` 图鉴奖励/时装收集日奖） | 卡片侧只保留一份目录 `projects/yslzm/catalog.py`；标题与原看板一致 |
| 9 | 中 | **依赖缠绕与硬编码路径**：控制器导入 `business.run_lock` 与 `tools.run_scheduled`，后者又在函数内反向导入 `tools.daily_board.stream`；导入失败时回退加载 `~/Documents/workspace/yslzm/tools/result_diagnostics.py` | 独立包，对 yslzm 业务代码零导入，只通过子进程协议交互；路径集中在适配器并可由 CLI 覆盖 |
| 10 | 低 | 页头「执行中」只看 `_running` 标志，而阶段事件会重算该标志，任务行在执行时页头可能不显示执行中 | 页头综合阶段与任务状态判定 |
| 11 | 低 | `stream.run_streamed` 在管道关闭后 `child.wait()` 无超时 | 等待受同一截止时间约束 |
| 12 | 低 | 遗留 webhook 摘要（`notify_daily_result.py`，1247 行）README 称不再使用，但 `run_scheduled.main()` 仍会调用 | 不迁移；旧 `run_scheduled.py` 调用方经兼容包装转入新控制台 |
| 13 | — | **卡片外观不是 todolist**：三列 `column_set` + ○◉!? 符号，等待/阻塞/未开始/部分完成都显示同一个 ○；已完成项全部藏进折叠面板，看不到当天全貌；任务不分阶段 | 重写为原生勾选器（`checker`）todolist：完成项打勾 + 删除线，按阶段分组且完成的分组自动折叠，失败原因紧跟条目，页头状态色/标签，会话列表摘要 |

## 已知限制（保持原语义，未改动）

- 发卡 UUID 一小时窗口过后，如果历史查询仍找不到卡片，当天不会再新建（防重复优先）；之后每次重试都会查询一次历史。历史查询需要机器人具备读取群消息的权限。
- 勾选框没有配置回调：飞书允许本地勾选但不会回传，卡片下次更新时恢复为服务器事实。
- yslzm 适配器的操作指标（`actions_*`）由原生结果记录近似换算，精确值以协议接入项目为准。
