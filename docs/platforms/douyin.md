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
- `data=[]` 但 `has_more=1` 是可继续的空批次，必须携带响应 `logid` 请求下一页并计入连续
  停滞；只有 `has_more=false`、缺失继续游标或达到连续停滞上限时才能停止。
