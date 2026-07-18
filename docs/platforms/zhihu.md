# 知乎

- 登录确认后先进入本轮真实关键词搜索页，再执行共享行为阶段并刷新搜索页 cookie；行为
  证据 URL 不含本轮关键词，或行为与请求策略证据缺失时，不得入库。

- 正式入口：`crawl_runner.py` 调用 MediaCrawler 知乎搜索。
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
- 去重键：内容 ID；answer URL 同时包含 question ID。
- 有 checkpoint 时先刷新配置的顶部页，再从保存页码继续。已知内容 ID 不再写入当前 JSONL；
  完整页保存下一页，页面中途停止保存当前页，顶部刷新不推进深层 checkpoint。
- 未达标摘要由 runner 自动累计，达到完整目标后一次性入库；正常 workflow 不使用人工
  `--start-page` 或 `--resume-summary`。
- 对一组已知 answer/article URL 重新核验详情时，使用根项目执行器的
  `--zhihu-detail-urls-file <JSON数组文件> --no-import` 诊断模式；该模式串行复用同一详情并发门禁，
  不写正式 checkpoint、不入库，也不能作为正式轮次完成证据。不要直接运行第三方内部命令。
- 首页、cookie reload 和搜索页导航的 `domcontentloaded` 超时为软失败；继续用已验证 cookie
  和 API client 检查。最终 API/字段失败仍按正式状态报告，不能仅因导航超时宣布失败或成功。
