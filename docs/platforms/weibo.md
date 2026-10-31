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
- 启动浏览器时不强制 User-Agent，视口与 warmup 相同（1280×900），同一持久 profile 登录与抓取时的
  浏览器 UA 一致；标准模式持久 profile 与 warmup 共用同一套启动配置（沙箱开启、`zh-CN` 与
  `Asia/Shanghai`），首页在持久 context 自带的第一页打开（#136）；`m.weibo.cn` 接口与图片请求头的 `User-Agent` 从 `navigator.userAgent` 读取，
  不再用固定 iPhone Safari（#127）。桌面 UA 下 `/api/config` 已由 warmup 验证；`/api/container/getIndex`
  在桌面 UA 下的 `cards` 结构待正式轮实证，结构不同或被拒只会运行级停止，不写错误数据。
- worker 启动时先用 `/api/config` 确认移动端登录；未确认时执行登录流程并刷新移动端 Cookie，之后必须
  再确认一次。仍未登录即记 `adaptive_search_stopped(runtime_failed, login_required)` 并结束，不发出
  任何搜索请求，不能把未登录状态下的空结果当作来源耗尽。

## 请求失败分类

- 移动接口（`/api/config`、搜索、长文详情）返回 401/403 记 `login_required`，429 记 `rate_limited`，
  418/432 风控拦截记 `blocked_or_forbidden`；这些都是运行级阻断，不重试，也不再在每次失败后跳转
  首页刷新 Cookie。
- 只有传输层异常（超时、连接错误）和 5xx 才在原预算内重试（搜索 5 次/3 秒，详情 3 次/1 秒），
  用尽后原样上抛。搜索阶段的任何失败都写 `runtime_failed`（阻断写对应错误码，传输层失败写
  `network_unreachable`，其余写 `search_request_failed`），不得当作空页。
- 重试用尽后仍是传输层失败（DNS、连接失败、超时、连接重置）是网络整体故障，与候选无关（#129）：
  长文详情不再落入 `unexpected_detail_failure` 候选跳过，而是以 `runtime_failed/network_unreachable`
  停止本轮，不写 `candidate_skipped`、不进 seen；指定帖子详情也不把它吞成空结果。runner 记为
  `network_timeout` 并等待 900 秒重试。正文图片下载仍按 `image_download_retryable`。
- 日志与异常只记状态码、`ok`、`msg` 与响应长度，不写响应原文；微博响应含 `avatar_hd`、
  `profile_image_url` 等头像字段。

## 字段来源

- 普通微博正文来自完整搜索 `mblog.text`，保存
  `content_detail_status=detail_observed`、`content_detail_source=search_mblog_complete`。正文写出前先把
  `<br>` 转为换行，再去标签，最后还原 HTML 实体。
- 发帖时间只来自 `mblog.created_at`（RFC 2822），换算为 Unix 时间戳与上海时区时间；缺失或无法解析时
  该候选写 `candidate_skipped(error_code=created_at_unparseable)`，不写出记录，搜索继续。
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

图片请求只直连新浪图床：先请求 `large` 规格，失败或超限时退回平台原始地址。请求头只含浏览器
User-Agent（与接口请求同一 `navigator.userAgent`）、`Referer: https://m.weibo.cn/` 和图片 Accept，不带登录 Cookie、Origin 或 Content-Type，
也不经任何第三方图片代理。

## 发现与批次诊断

只有搜索响应的原始 `cards` 为空（含平台“这里还没有内容”空页），或页面没有微博卡片且带“暂无内容”
这类明确无内容标记时，才写 `source_exhausted(empty_page)`。有卡片但都不是 `card_type=9`（话题、
用户卡、推荐卡）是正常页，按批次记录后继续翻页。

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
`$render_data` 不存在时，修复入口打开同一登录浏览器中的精确 detail 页，先看导航响应状态
（401/403 记 `login_required`，429 记 `rate_limited`，其他非 2xx 视为该条详情不可用），再读取结构化
页面状态，仍要求目标 ID 完全一致且正文非空；不调用 Playwright `page.request` 等请求接口。
该入口不写搜索 checkpoint，完成含义只覆盖清单中的旧行。
