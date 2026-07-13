# 微博

- 正式入口：`crawl_runner.py` 调用 MediaCrawler 微博搜索。
- 粉丝来源：搜索结果 `mblog.user` 的 `followers_count/fans_count` 系列字段。
- 图片来源：`mblog.pics`；没有正文图片的记录不是有效图文。
- 去重键：微博 ID。
- 粉丝量为 0 时，只有原始 user 对象明确包含粉丝字段才有效。
- 搜索结果数量按实际内容记录计入候选硬上限。
