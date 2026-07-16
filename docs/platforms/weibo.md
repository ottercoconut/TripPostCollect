# 微博

- 持久化登录先在后台移动页验证 `/api/config`；失效时另开并置前
  `passport.weibo.com/sso/signin` 桌面 SSO 登录页。检测到 SSO 登录后再导航到移动端刷新
  Cookie；关闭重开 profile 后必须同时满足 `login=true` 和有效 `uid`。不要在无登录入口
  的移动验证页等待人工操作，也不要把桌面域 `WBPSESS` 单独视为移动端登录成功。
- 登录确认后先进入本轮真实关键词的移动搜索页，再执行共享行为阶段并刷新移动端 cookie；
  行为证据 URL 不含本轮关键词，或行为与请求策略证据缺失时，不得入库。

- 正式入口：`crawl_runner.py` 调用 MediaCrawler 微博搜索。
- 粉丝来源：搜索结果 `mblog.user` 的 `followers_count/fans_count` 系列字段。
- 图片来源：`mblog.pics`；没有正文图片的记录不是有效图文。
- 去重键：微博 ID。
- 粉丝量为 0 时，只有原始 user 对象明确包含粉丝字段才有效。
- 搜索结果数量按实际内容记录计入候选硬上限。
