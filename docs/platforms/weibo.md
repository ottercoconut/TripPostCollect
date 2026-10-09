# 微博

微博正式抓取服从[正式抓取执行契约](../formal-crawl-contract.md)。本文只维护移动端登录、长文、候选
发现诊断和图片字段差异。

## 登录与入口

- 正式入口：`crawl_runner.py` 经 `mediacrawler_crawl.py` 启动根包 worker（`trippostcollect.platforms.entry`）
  执行微博搜索，实现位于 `src/trippostcollect/platforms/weibo/`。
- warmup 用后台移动页验证 `/api/config`；失效时把桌面
  `passport.weibo.com/sso/signin` 登录页置前。人工完成 SSO 后回到移动端刷新 Cookie。
- 关闭重开 profile 后，只有移动接口同时返回 `login=true` 和有效 `uid` 才成功；`WBPSESS` 不能
  单独作为登录证据。
- 行为阶段必须在本轮真实关键词移动搜索页完成并刷新移动端 Cookie。

## 字段来源

- 普通微博正文来自完整搜索 `mblog.text`，保存
  `content_detail_status=detail_observed`、`content_detail_source=search_mblog_complete`。
- `isLongText=true` 必须请求移动详情并使用 `content_detail_source=mobile_detail`；详情候选级失败
  有限重试后写 `candidate_skipped(failure_scope=post)`，不得保存截断搜索文本。
- 正文图片只来自 `mblog.pics` 归一后的 `image_list`，来源键为 `image_list`；头像、用户对象图片、
  封面和视频缩略图不进入正文候选。
- 作者粉丝来自 `mblog.user` 的明确粉丝字段，保存
  `followers_observed=true`、`author_followers_source=search_author`。原始字段明确为 0 时才接受 0。
- 作者平台 ID 保存 `mblog.user.id` 原始值（整数归一为字符串，写在 `creator_hash` 键），昵称保存
  `screen_name` 原文；头像、性别、主页、签名和 IP 归属地不写出。
- 去重键为微博 ID；没有正文图片的记录不是有效图文。

有 `pid` 时图片稳定资产键为 `weibo:pid:<pid>`，否则回退到规范 URL 哈希。平台层复用移动搜索会话
下载并写 staging/manifest，通用重试、失败分类、根项目复验和事务见
[正式契约](../formal-crawl-contract.md)与[数据持久化](../data-persistence.md)。

## 发现与批次诊断

综合搜索的批次停滞计数按“是否出现未知微博 ID”计算，而不是本页是否有有效图文。批次证据使用
`stagnation_basis=candidate_identity`；该计数只用于诊断，不触发停止。纯文本或视频页仍继续扫描，
正式完成只接受平台明确返回的来源耗尽证据。

有 checkpoint 时先刷新顶部，再从保存页码继续。数据库、累计摘要、seen、人工排除和本轮已见 ID
在长文详情与图片请求前过滤；完整页保存下一页，页中途停止保存当前页，顶部刷新不推进深层位置。

微博来源耗尽轮常因正文图片数量大而长时间运行。统一的 1200 秒无持久进展看门狗只限制 execution
state、内容 JSONL、图片 manifest 和行为证据同时无变化的持续时间，不限制整轮总时长；持续完成候选或
图片时不得仅因累计运行时长结束进程。触发 `no_progress_timeout` 时只保留最后完整批次的安全恢复位置，旧 staging
不得导入，账号健康状态也不得据此改变。

历史库中缺少 `content_detail_status=detail_observed` 的既有记录，从根项目
`scripts/repair_post_details.py --platform weibo` 分批修复。清单把既有微博 ID 与
`https://m.weibo.cn/detail/<id>` 严格绑定；detail 模式先在本轮青岛关键词移动搜索页完成人类行为
证据，再从移动详情标记 `content_detail_source=mobile_detail` 并下载 `mblog.pics` 正文图。旧式
`$render_data` 不存在时，修复入口可以从同一登录浏览器打开的精确 detail 页读取结构化页面状态，
但仍要求目标 ID 完全一致且正文非空。新浪原图经代理失败、超限或旧 URL 已经带 `i1.wp.com` 时，
图片客户端只在规范代理 URL 与原始新浪 `large` URL 间有限回退，禁止重复包装代理地址。该入口不写
搜索 checkpoint，完成含义只覆盖清单中的旧行。
