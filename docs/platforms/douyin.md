# 抖音

- 登录确认后先进入本轮真实关键词搜索页，再执行共享行为阶段并刷新 API 客户端 cookie；
  行为证据 URL 不是该搜索页，或行为与请求策略证据缺失时，不得入库。

- 正式入口：`crawl_runner.py` 调用 MediaCrawler 抖音搜索。
- 当前正式数量只读取 `config/crawl_targets.json` 中该 job 的 `target_new_posts` 和
  `candidate_hard_limit`：达到有效新增目标即提前停止，否则最多累计候选硬上限后结束本轮。
- 只接受图文作品；视频作品计入跳过候选。
- 粉丝来源：图文作者主页，搜索作者对象只在原始字段明确存在时作为来源。
- 作者补全预算必须随候选硬上限传入，不得使用固定 30 作者上限。
- 粉丝量为 0 只有 `followers_observed=true` 才有效；旧版可疑 0 不得通过校验。
- 图片来源：图文作品的 note/image 列表。
- 去重键：`aweme_id`。
- 自适应循环持续分页，直到有效目标、候选硬上限、数据源耗尽或连续停滞。
- 搜索接口每次请求 15 条，offset 必须按 15 递增；响应 `logid` 作为下一页 search ID。
  页级状态记录 page、offset/search ID、原始返回条数和 `has_more`。响应缺少 `data` 是
  运行/风控失败，不得写成数据源耗尽。
- SQLite checkpoint 同时保存下一 page、offset 和响应 `logid`。自动恢复必须把三者一起传给
  child；只有 `--start-page` 或 offset、但 search ID 为空时执行器直接拒绝，不能把这种深页
  请求的空结果解释为来源耗尽。
- 有 checkpoint 时先用空 cursor 从顶部刷新配置页数，再用保存的三元组进入深层前沿。已知
  `aweme_id` 在作者主页补全和媒体处理前跳过；顶部刷新不覆盖深层 cursor。
- `status=exhausted` 只证明已保存 search ID 的游标链结束。顶部刷新若观察到至少一个不在数据库、
  累计摘要或 `crawl_discovery_seen_candidates` 中的新 `aweme_id`，并且最后一页返回
  `has_more=true` 与非空 `logid`，从刷新链下一页
  建立新 cursor 前沿，写 `discovery_frontier_reseeded` 证据并由新三元组替换旧耗尽 checkpoint。
  没有新候选 ID 或连续游标时保持耗尽，仅刷新顶部。新前沿遇到历史视频、字段无效项或有效项时
  由持久候选集合在作者主页和媒体处理前跳过，不消耗候选预算。
  顶部刷新本身已经 `target_new_met` 时不 reseed，立即结束并保留旧耗尽 checkpoint。
- 完整响应保存下一 page、`offset + 15` 和下一 `logid`；在目标或候选上限处中途停止时保存当前
  请求三元组，下一次重取边界批次。未达标摘要自动累计，正常 workflow 不使用人工恢复参数。
- `data=[]` 但 `has_more=1` 是可继续的空批次，必须携带响应 `logid` 请求下一页并计入连续
  停滞；只有 `has_more=false`、缺失继续游标或达到连续停滞上限时才能停止。
