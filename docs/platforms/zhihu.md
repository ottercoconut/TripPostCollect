# 知乎

- 登录确认后先进入本轮真实关键词搜索页，再执行共享行为阶段并刷新搜索页 cookie；行为
  证据 URL 不含本轮关键词，或行为与请求策略证据缺失时，不得入库。

- 正式入口：`crawl_runner.py` 调用 MediaCrawler 知乎搜索。
- 完成模式服从正式契约：正常默认使用 `target-new-posts`；只有用户明确要求某一轮直到来源耗尽时，
  才在该轮命令临时使用 `source-exhausted`，不修改长期配置。
- 登录态：必须存在经重开验证的 `d_c0/z_c0` cookie snapshot。
- 内容类型：answer 和 article；zvideo 跳过。
- 粉丝来源：搜索响应 author/member 的 `follower_count`，同时保存
  `followers_observed` 和 `author_followers_source=search_author`。
- 缺失粉丝字段不得使用模型默认 0 通过校验。
- 图片来源：正文 HTML 中的正文图片，公式图片不计入。搜索响应没有正文图时，正式搜索会按内容
  ID 低频请求回答/文章详情并重新解析；只合并详情正文与图片，继续保留搜索响应中的作者和粉丝
  证据。详情 HTTP 请求成功，且页面 `js-initialData` 中能解析出 answer/article entity 并构造非空
  内容对象时，JSONL 才写 `content_detail_status=detail_observed`；该状态不依赖“最终是否有图”。
  此时仍无图片才记为
  `missing_content_image`；`request_failed`、`parse_failed` 或旧产物缺少该字段统一记为
  `content_detail_unobserved`，不能解释成内容真实无图。
- 正文来源：搜索对象同时含非空完整 `content` 和正文图时保存
  `content_detail_source=search_content`；否则必须请求详情并保存 `answer_detail` 或
  `article_detail`。`title`、`desc/excerpt` 不能单独通过。详情请求或解析失败记录
  `content_detail_failed`，保留原页；该内容 ID 不进入 `crawl_discovery_seen_candidates`。
- 去重键：内容 ID；answer URL 同时包含 question ID。
- 有 checkpoint 时先刷新配置的顶部页，再从保存页码继续。已知内容 ID 不再写入当前 JSONL；
  完整页保存下一页，页面中途停止保存当前页，顶部刷新不推进深层 checkpoint。
- 默认数量模式的未达标摘要由 runner 自动累计，达到完整目标后一次性入库；显式来源耗尽模式在
  取得可验证耗尽证据后入库。正常 workflow 不使用人工
  `--start-page` 或 `--resume-summary`。
- 对一组已知 answer/article URL 重新核验详情时，使用根项目执行器的
  `--zhihu-detail-urls-file <JSON数组文件> --no-import` 诊断模式；该模式串行复用同一详情并发门禁，
  不写正式 checkpoint、不入库，也不能作为正式轮次完成证据。不要直接运行第三方内部命令。
- 首页、cookie reload 和搜索页导航的 `domcontentloaded` 超时为软失败；继续用已验证 cookie
  和 API client 检查。最终 API/字段失败仍按正式状态报告，不能仅因导航超时宣布失败或成功。

## 正文图片本地化

知乎只从 answer/article 正文 HTML 归一化后的 `image_list` 生成候选。搜索正文已有图片时直接使用；
缺图时必须先完成详情补全，只有 `content_detail_status=detail_observed` 的最终正文图集合才进入下载。
`zvideo`、`/equation` 公式图、`avatar_url`、作者主页资源、封面和搜索预览都在候选阶段排除，不触发
图片请求。

图片字节请求复用当前知乎登录会话。每帖全部图片经真实格式、解码和大小检查后原子写入
`<platform_artifact>/data/zhihu/images/<content_id>/<index>.<real_ext>`，manifest 位于
`<platform_artifact>/data/zhihu/image_manifest.jsonl`，来源字段为 `image_list`、角色为 `content`。
同帖 `zhimg.com` URL 先按去除 `_r`、`_720w`、`_1440w` 等已知变换后缀的资源路径去重，保留
首次 URL 并连续编号；原始 `image_list` 继续保留全部 URL 证据，被合并变体不重复下载或写 manifest。
该规则只识别知乎稳定 URL 语义，不做感知哈希或视觉相似比较。失败行使用
`image_download_retryable` 或具体格式错误码。空响应或超时先按单图最多 3 次、1–2 秒随机基数指数
退避重试；日志逐次记录，成功或最终失败的 manifest `attempts` 保存实际请求次数。只有三次后仍为
`image_download_retryable` 时写
`candidate_deferred`，暂时跳过该回答/文章并继续后续候选；失败 ID 不进入候选记忆，checkpoint 回到
最早失败页。该帖不得把详情失败、部分成功或公式图排除解释成图片完成。
格式、解码、大小或明确非重试 HTTP 等终态错误写失败 manifest 后停止当前页，不写 deferred。

根执行器按 `image_list` 投影后的候选顺序复验 manifest 和文件，正式运行晋升到 `data/media/zhihu/...`
前仍在同一帖子内按验证后的 SHA-256 做字节级兜底去重并连续重编号；随后在同一 SQLite 事务写
本地证据。
`request_failed`、`parse_failed` 和
`content_detail_unobserved` 均不能进入成功态；`--no-import` 只保留 staging/manifest，不写长期
目录或数据库。
