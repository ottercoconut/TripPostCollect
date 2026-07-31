# 抖音

- 登录确认后先进入本轮真实关键词搜索页，再执行共享行为阶段并刷新 API 客户端 cookie；
  行为证据 URL 不是该搜索页，或行为与请求策略证据缺失时，不得入库。
  API 客户端在进入关键词页之前监听浏览器搜索响应；搜索循环优先复用浏览器已经取得的首页和
  行为滚动分页，避免对同一 offset/search ID 重复请求。只有超过浏览器已加载前沿时才发出后续
  API 请求。同一关键词、offset 和 search ID 若观察到多条响应，视为预取/预测与正式响应竞态：
  合并去重并优先使用最新的非 `verify_check` 响应；只有全部响应均为验证信号时才按风控失败停止。
  首页流响应无法读取或额外首页请求返回 `verify_check` 时，只有页面至少存在 10 个可见
  `waterfall_item_*` 作品卡、且浏览器已取得同一关键词 offset 10 的健康响应和非空 search ID，
  才允许用前 10 个可见作品 ID 的详情重建首页，并从该 search ID 继续。任一证据不足仍按风控
  失败停止，不能跳过首页或猜测 cursor。

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
- 搜索接口当前使用站内单列搜索契约：新鲜首页调用 `/general/search/stream/`，后续携带 search ID
  的分页调用 `/general/search/single/`。首页响应可能保留原始 HTTP chunk 边界，必须重组所有 chunk
  后再解析 JSON。两类请求均使用 `count=10`、`list_type=single`、
  `search_source=normal_search`；每次请求 10 条，offset 必须按 10 递增。首页以浏览器 offset 10
  请求携带的 search ID 建链；可正常解析首页流时，也可用其 `extra.logid` 建链。后续页始终复用
  已建立的 search ID，不把每页响应的轮换 `logid` 当作新 cursor。不得复用旧版固定
  `from_group_id` 或 `count=15/list_type=multi` 组合，否则接口可能
  返回空数组，而同一浏览器搜索页仍显示结果。
  页级状态记录 page、offset/search ID、原始返回条数和 `has_more`。响应缺少 `data` 是
  运行/风控失败，不得写成数据源耗尽。
- 搜索 JSON 必须通过业务 envelope 校验：非成功 `status_code`、`data` 不是列表、缺失或非法
  `has_more`，以及 `has_more=true` 但没有下一 `logid`，都停止为 `runtime_failed`。状态文件只保存
  `status_code`、数据条数、`has_more` 和 `logid` 是否存在等脱敏元数据，不保存完整响应。
  `search_nil_info.search_nil_type=verify_check` 是显式风控信号，必须停止为
  `search_verify_check`，不得解释成来源耗尽。
- 新鲜游标链的第 1 页（page 1、offset 0、空 search ID）若返回
  `data=[]、has_more=false`，必须检查当前可见搜索页。存在作品链接或搜索结果卡片时停止为
  `empty_api_response_with_visible_results`；新版无作品链接的 `.search-result-card` 和
  `waterfall_item_*` 瀑布流节点也属于可见卡片。既无卡片也无明确可见“无结果”文案时停止为
  `ambiguous_empty_first_page`。这两种情况均为运行/风控失败，checkpoint 保持第 1 页不前移。
  只有可见页面明确显示无结果，才允许 `verified_empty_first_page` 证明来源耗尽。
- SQLite checkpoint 同时保存下一 page、offset 和稳定 search ID。自动恢复必须把三者一起传给
  child。这里的 search ID 是首页建链后浏览器后续请求持续复用的会话 ID，不是每页响应随请求
  变化的 `extra.logid`；后续页必须保留请求携带的原 search ID。旧失败摘要若已把轮换 log ID
  写入 checkpoint，runner 只允许从冻结摘要第一个 frontier batch 的建链证据纠正，且在 dry-run
  中标记 `resume_cursor_corrected_from_summary=true`。只有 `--start-page` 或 offset、但 search ID
  为空时执行器直接拒绝，不能把这种深页
  请求的空结果解释为来源耗尽。
- 有 checkpoint 时先用空 cursor 从顶部刷新配置页数，再进入保存的深层 page/offset。抖音
  search ID 是浏览器搜索会话级 cursor，不能跨进程照搬：只有本轮顶部刷新已经取得健康后页、
  `has_more=true` 和非空稳定 search ID，才用该 ID 重新绑定深层前沿；page/offset 必须原样保留，
  并写 `douyin_frontier_cursor_rebound` 证据。没有健康刷新链时保持旧 checkpoint 并失败，不能猜 ID。
  已知 `aweme_id` 在作者主页补全和媒体处理前跳过。
- `status=exhausted` 只证明已保存 search ID 的游标链结束。顶部刷新若观察到至少一个不在数据库、
  累计摘要或 `crawl_discovery_seen_candidates` 中的新 `aweme_id`，并且最后一页返回
  `has_more=true` 与非空稳定 search ID，从刷新链下一页
  建立新 cursor 前沿，写 `discovery_frontier_reseeded` 证据并由新三元组替换旧耗尽 checkpoint。
  没有新候选 ID 或连续游标时保持耗尽，仅刷新顶部。新前沿遇到历史视频、字段无效项或有效项时
  由持久候选集合在作者主页和媒体处理前跳过，不消耗候选预算。
  顶部刷新本身已经 `target_new_met` 时不 reseed，立即结束并保留旧耗尽 checkpoint。
- 完整响应保存下一 page、`offset + 10` 和稳定 search ID；在目标或候选上限处中途停止时保存当前
  请求三元组，下一次重取边界批次。未达标摘要自动累计，正常 workflow 不使用人工恢复参数。
- `data=[]` 但 `has_more=1` 是可继续的空批次，必须携带已建立的稳定 search ID 请求下一页并计入连续
  停滞；深层页的 `has_more=false` 可以证明当前游标链耗尽。缺失继续游标属于响应异常；新鲜第
  1 页的 `has_more=false` 还必须满足上面的可见无结果门禁，不能只凭接口空数组停止。
