# 小红书

- 正式入口：`crawl_runner.py` 调用 MediaCrawler 小红书搜索。
- 登录态：必须通过 `mediacrawler_login_warmup.py` 生成并复验 storage snapshot。
- 粉丝来源：每条图文的作者主页补全；搜索分支必须启用
  `TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS=1`。
- 图片来源：笔记详情 `image_list`；视频笔记在详情阶段跳过。
- 去重键：笔记 ID。
- 作者主页请求失败时保留候选和失败原因，但记录不能进入有效集合。
- 自适应循环按实际搜索候选、有效唯一数和连续停滞批次停止。
