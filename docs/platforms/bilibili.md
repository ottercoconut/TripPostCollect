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
  分支，但与其它通用平台共用 SQLite 发现 checkpoint 和跨次累计摘要。
- 首次从第 1 页开始；有 checkpoint 时先刷新 `top_refresh_max_pages` 个顶部页，再从
  `resume_page` 继续。数据库、累计摘要、`crawl_discovery_seen_candidates` 或本次已见的 article ID 在作者粉丝接口前跳过，不消耗
  单次未知候选预算。
- 每个完整深层页保存下一页；达到目标或候选上限时若页面尚未处理完，保存当前页，下一次允许
  重取并靠已知 ID 跳过已持久化边界。空页保存 `status=exhausted`，后续只刷新顶部。
- 未达标产物不单独入库；runner 自动拼接摘要，累计达到完整目标后一次性导入。人工
  `--resume-summary` 或 `--start-page` 不是正常 workflow。
- B站当前按页面是否出现成功归一化且数据库、累计摘要、`crawl_discovery_seen_candidates` 和本轮均未见的 article ID 累计停滞；
  这类未知 ID 即使后续正式字段校验无效，也会重置停滞页数。该平台例外不改变完成判据，判断是否
  扩容时必须同时检查新 ID 数和有效新增数。
- 临时调整候选预算必须遵循运行手册的配置校验、`--sync-only`、dry-run、正式执行和恢复原值
  顺序；候选预算是单次 child 预算，不是跨次累计总额。
