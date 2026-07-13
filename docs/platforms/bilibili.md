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
