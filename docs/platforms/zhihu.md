# 知乎

- 登录确认后先进入本轮真实关键词搜索页，再执行共享行为阶段并刷新搜索页 cookie；行为
  证据 URL 不含本轮关键词，或行为与请求策略证据缺失时，不得入库。

- 正式入口：`crawl_runner.py` 调用 MediaCrawler 知乎搜索。
- 登录态：必须存在经重开验证的 `d_c0/z_c0` cookie snapshot。
- 内容类型：answer 和 article；zvideo 跳过。
- 粉丝来源：搜索响应 author/member 的 `follower_count`，同时保存
  `followers_observed` 和 `author_followers_source=search_author`。
- 缺失粉丝字段不得使用模型默认 0 通过校验。
- 图片来源：正文 HTML 中的正文图片，公式图片不计入。
- 去重键：内容 ID；answer URL 同时包含 question ID。
- 首页、cookie reload 和搜索页导航的 `domcontentloaded` 超时为软失败；继续用已验证 cookie
  和 API client 检查。最终 API/字段失败仍按正式状态报告，不能仅因导航超时宣布失败或成功。
