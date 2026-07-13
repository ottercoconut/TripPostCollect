# 小红书

- 正式入口：`crawl_runner.py` 调用 MediaCrawler 小红书搜索。
- 登录态：必须通过 `login_warmup.py --targets xhs` 生成并复验 storage snapshot。
- 粉丝来源：每条图文的作者主页补全；搜索分支必须启用
  `TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS=1`。
- 图片来源：笔记详情 `image_list`；视频笔记在详情阶段跳过。
- 去重键：笔记 ID。
- 作者主页请求失败时保留候选和失败原因，但记录不能进入有效集合。
- 自适应循环把同一 `search_id` 和递增 `page` 传给搜索接口，按实际搜索候选、有效唯一数
  和连续停滞批次停止；单页只有推荐查询项时继续下一页，不能误报数据源耗尽。
- 页级状态必须记录 `source_page`、`search_id`、原始返回条数和 `has_more`。只有空响应或
  `has_more=false` 才能标记 `source_exhausted`；缺少停止事件视为运行失败。
