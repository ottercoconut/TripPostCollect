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
- 综合搜索的停滞按是否出现不在数据库、累计摘要、`crawl_discovery_seen_candidates` 和本 child 已见集合中的微博 ID 计算，不按有效图文数计算。纯文本和视频仍是无效
  图文，但只要页面持续提供新 ID，就继续扫描至有效新增目标、候选硬上限或明确空页；连续配置
  批次没有新 ID 才记为 `stagnated`。批次证据中的 `stagnation_basis=candidate_identity` 必须与此一致。
- 有 checkpoint 时先刷新配置的顶部页，再从保存页码继续。已知微博 ID 在批量全文请求、图片
  和存储前过滤；完整页保存下一页，页面中途停止则保存当前页。顶部刷新不推进深层页码。
- 未达标摘要由 runner 自动累计；正常 workflow 不需要人工传 `--start-page` 或
  `--resume-summary`。明确空页后 checkpoint 标记耗尽，后续只刷新顶部。
