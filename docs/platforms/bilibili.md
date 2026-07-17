# B站 article

- 正式入口：`crawl_runner.py` 调用 `mediacrawler_crawl.py --platforms bilibili`。
- 内容来源：B站 article 搜索；不使用视频搜索。
- 粉丝来源：按 article 的作者 `mid` 调用作者关系统计接口，保存数值、
  `followers_observed` 和 `author_followers_source=relation_stat`。
- 图片来源：article 搜索结果的 `image_urls`。
- 去重键：article 内容 ID。
- 有效性：必须满足 `image_post_with_followers_v1`，粉丝统计失败的 article 继续作为候选，
  但不能进入有效集合。
- 单页 Opus 抓取只用于定向页面证据，不代表正式平台轮次。
- article API 搜索前必须在 MediaCrawler 持久 profile 执行共享行为阶段，并复用该会话
  cookie；行为与请求策略证据缺失时不得入库。
- 配置中的 job kind 虽为 `mediacrawler_search`，B站内容抓取实际使用项目自有 article API
  分支，不属于当前支持冻结断点续跑的 MediaCrawler 分页平台。
- 当前不支持冻结断点续跑。B站新完整轮次的 dry-run 和正式命令均不得传入
  `--resume-summary`、`--recovery-keyword` 或 `--start-page`，由执行器默认从第 1 页开始。
  未达标轮次不入库；只有停止证据为 `candidate_hard_limit_reached` 且没有来源耗尽证据时，才按
  页级证据调整配置中的候选预算，再启动新的完整正式轮次。
- 旧轮产物及其计数不并入新轮去重集合；新轮从零累计 `candidate_count`、`valid_new_count` 和
  `valid_existing_count`，其中只有新轮启动时 SQLite 已存在的身份才计入
  `valid_existing_count`。
- B站当前按页面是否出现成功归一化且本轮未见的 article ID 累计停滞；这类新 ID 即使后续
  正式字段校验无效或数据库已有，也会重置停滞页数。该平台例外不改变完成判据，判断是否
  扩容时必须同时检查新 ID 数和有效新增数。
- 临时调整候选预算必须遵循运行手册的配置校验、`--sync-only`、dry-run、正式执行和恢复原值
  顺序；dry-run 的实际命令中不得出现任何恢复参数。
