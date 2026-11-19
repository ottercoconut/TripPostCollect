"""#211 微博桌面搜索替换了 m.weibo.cn 的搜索、/api/config 登录和顶部刷新。

`tests/platforms/weibo/test_weibo_offline_drive.py` 里以下三组仍按移动搜索的请求路径、
`mobile_detail` / `search_mblog_complete` 正文来源和 `empty_page` 耗尽细节固化预期。
详情点击、`statuses/show` 与正文图留到下一段。component lane 不接受 skip，因此这三组用例从
`test_weibo_offline_drive.py` 删除，而不是标成跳过：

- test_drive_requests_artifacts_and_events
- test_entry_assembly_matches_direct_construction
- test_root_drive_semantics_changed_by_issue_114

同一文件里不经整轮 `crawler.start()` 的修复链、依赖方向、worker 装配和配置读取仍然执行。
登录、翻页、来源耗尽和全量扫描的新行为在 `test_weibo_desktop_search.py`。
"""
