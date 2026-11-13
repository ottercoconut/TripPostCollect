# 知乎

知乎正式抓取服从[正式抓取执行契约](../formal-crawl-contract.md)。本文只维护知乎的登录、详情、字段和
图片资产差异。

## 入口与类型

- 正式入口：`crawl_runner.py` 经 `mediacrawler_crawl.py` 启动根包 worker（`trippostcollect.platforms.entry`）
  执行知乎搜索，实现位于 `src/trippostcollect/platforms/zhihu/`。
- 登录态必须具有经关闭重开复验的 `d_c0/z_c0` Cookie snapshot。
- 持久 profile 是知乎登录态的唯一权威来源。worker 不向浏览器注入快照 Cookie（注入会用无
  expires/httpOnly/secure 的副本覆盖 profile 中的实时 `z_c0`）。登录复验只截获页面自己发出的
  `/api/v4/me`（首页和搜索页加载时都会自己请求它，#185）：截获不到，或响应不是 HTTP 200 且同时带
  `uid` 与 `name`，即以 `runtime_failed/login_required` 运行级阻断结束，不在本轮重新登录；操作人先运行
  `scripts/login_warmup.py --platforms zhihu` 再重跑。详情从点击打开的新标签读取页面状态（#185）。
  正文图仍用浏览器外 CDN：UA 与 Referer 取自浏览器，不带 Cookie，不经第三方代理。搜索、详情和作者都改为
  页面取数之后，图片客户端不再读取或保存 Cookie（#185）。
- 启动浏览器时不强制 User-Agent，浏览器自报真实 UA；httpx 请求头的 `user-agent` 在首页加载后经已登记的
  状态脚本读取 `navigator.userAgent`，与 warmup 登录时同一浏览器一致，不写死版本串（#127）。正式轮走 CDP，
  复用持久 profile 的默认 context；外部 Chrome 以 `--accept-lang=zh-CN`、原生窗口 1450×900 和
  `TZ=Asia/Shanghai` 启动，标准模式及 CDP 失败回退也经共享 `launch_profile_context`，不再用 viewport
  模拟设备（#142）。行为证据 `runtime_fingerprint` 同时记录 `languages`、`timezone`、真实 screen 与 DPR。
- 只接受 answer 和 article；zvideo 在搜索解析阶段丢弃，只计为已处理候选并进入候选记忆，不写项目 JSONL。
- 去重键为内容 ID，answer URL 同时保留 question ID。
- 行为阶段必须在本轮真实关键词搜索页完成；页面导航超时可以作为软失败继续检查已验证 Cookie 和
  API，最终仍以 API、字段和阻断证据决定结果。

## 正文、作者与详情

- 每条未知候选都在搜索页点击标题，在新标签打开详情（#185）。回答页是
  `https://www.zhihu.com/question/{qid}/answer/{aid}`，文章页是 `https://zhuanlan.zhihu.com/p/{id}`。
  搜索结果里正文已经完整的候选也打开，正文、图片、点赞、评论、收藏、分享和发布时间都以详情页为准。
- 详情数据是新标签里的页面状态 `script#js-initialData`：回答取 `initialState.entities.answers.{id}`，
  文章取 `initialState.entities.articles.{id}`。点赞是 `voteupCount`，评论是 `commentCount`，收藏是
  `favlistsCount`（入库 `collected_count` → `post_favorites_count`），分享是
  `reaction.statistics.shareCount`（入库 `share_count` → `post_shares_count`）。这四项缺键为 NULL，
  不补 0；键上的 0 仍记为 0。缺任一项按字段 profile 判为不合格。
- 详情标签停留随机 5–9 秒（账本阶段 `note_detail`），然后在关闭前点击作者名。作者页
  `https://www.zhihu.com/people/{url_token}` 在新标签打开，数据仍是 `script#js-initialData` 的
  `initialState.entities.users.{url_token}`。点作者前先写一段 `creator_profile` 停顿（1–2 秒）；作者标签
  再停留随机 5–9 秒后关闭，回到详情标签，再关闭详情标签回到搜索页。点击、页面状态读取、停留和关闭都写入
  访问账本。`url_user_id` 取作者页 URL 的 token。
- 每条入库记录带 `content_visit_refs`：`ledger_id`、`search_results`、`note_detail`、`creator_profile`、
  `images`。访问阶段统一用这些链名；正文来源字段仍是 `answer_detail` 或 `article_detail`。知乎已纳入
  访问门禁，正式记录要过 `content_visit_ok`（#185）。指定 URL 的修复链没有搜索访问：详情由 `navigate`
  打开，仍读页面状态。
- 没有 `/people/{token}` 作者链接，或作者页状态里没有该用户，是候选级失败（`author_link_missing` /
  `author_profile_missing`），按 3 次、间隔 1 秒重试后写 `candidate_skipped` 并进入已处理记忆。作者页落到
  登录或安全验证，沿用详情页的同一组 URL 与可见特征，是运行级阻断。
- 详情只有在页面状态能解析出目标 entity 且正文非空时，才写 `content_detail_status=detail_observed`，
  来源为 `answer_detail` 或 `article_detail`。历史记录仍可能是 `search_content`。详情观察后仍无图片才是
  `missing_content_image`；`request_failed`、`parse_failed` 和旧产物缺状态都属于 `content_detail_unobserved`。
- `title`、`desc` 和 `excerpt` 不能替代正文。
- 正文 `content_text` 保留块级换行并剔除 figure/figcaption；图片说明文字不进入正文。
- 作者粉丝以作者页 `followerCount` 为准，`author_followers_source=author_page`，并要求
  `followers_observed=true`；缺键不能补 0。关注数取作者页 `followingCount`，缺键为 NULL，不补 0；键上的 0
  仍记为 0。作品数 `author_posts_count` 是 `answerCount + articlesCount`，两者任一缺失则为 NULL。
  `answerCount`、`articlesCount`、`pinsCount`、`questionCount` 四个原值写入 `author_json`，缺键不写、不补 0。
- 认证如何判定暂不改：仍取详情或搜索作者上的 `verify_bayes`，没有时取 `badge_v2.title`，`author_verified`
  保持 NULL。作者页用户实体上能看到的相关键是 `badgeV2`、`isOrg`、`userType`、`vipInfo`；`verifyBayes`
  在侦察的页面状态里没有出现。
- 作者平台 ID 保存 author/member 原始 `id`（写在 `creator_hash` 键），昵称保存 `name` 原文；
  `url_token` 另存为 `creator_url_token`，只用于拼接主页 `https://www.zhihu.com/people/<url_token>`，
  不作为平台 ID。
- 作者简介取 author/member 的 `headline`，写入 `author_description`。认证文字的取值见上一条，本段不改判定。
- 详情候选级失败（找不到标题卡片、新标签没打开、页面状态里没有目标实体、实体没有正文）按 3 次、
  间隔 1 秒有限重试，然后写 `candidate_skipped` 并进入已处理记忆。登录页、安全验证页，以及
  page/context 被关闭，是运行级阻断，不写 seen。登录与验证的 URL 和可见特征是待实测的假设，写在
  `detail_page.py` 的常量里。
- 搜索阶段的运行级失败保留各自错误码。搜索响应里的 401/403（`login_required`）和 429（`rate_limited`）
  仍不重试。
- 搜索和详情都不再走浏览器外 HTTP（#185），因此不再从 `request()` 产生 `network_unreachable`。
  正文图片下载仍按 `image_download_retryable`；图片请求的 401/403 与 429 仍是运行级
  `image_auth_required` / `image_rate_limited`。
- 日志只记录内容 ID、类型、计数和状态码；不打印整条记录、search_v3 原始响应或 `/me` 响应。作者头像
  只在内存中用于排除正文图候选，不进入 `model_dump` 和项目 JSONL。

## 正文图片与资产键

正文图只来自最终 answer/article HTML 归一后的 `image_list`。`zvideo`、`/equation` 公式图、头像、
作者主页、封面和搜索预览在候选阶段排除。

同帖 `zhimg.com` URL 按稳定资源路径移除 `_r`、`_<width>w` 等已知变换后缀，保留首次 URL 后再下载；
原始 `image_list` 保留所有 URL 证据。该规则不作用于外部域名，不使用感知哈希或视觉相似度。zhimg CDN
图片请求只发浏览器 UA（与 API 请求同一 `navigator.userAgent`）、图片类 Accept 和 `Referer: https://www.zhihu.com/`，不带 zhihu.com Cookie 和 API
签名头，也不经第三方代理。每张成功下载的正文图在访问账本以来源 `cdn_direct`、规则
`zhihu.content_image` 登记状态码、资产键和 sha256，供门禁 R5 与 manifest 对账。平台层写
staging/manifest，根项目再执行通用字节复验、同帖 SHA-256 去重、晋升和事务。

历史 manifest 中旧 zhimg URL 变体的严格兼容条件见[数据持久化](../data-persistence.md)；它只用于
恢复复验，不改变当前候选投影。

## 搜索响应

搜索只消费本轮搜索页自己发出的 `/api/v4/search_v3`（#185）。保留打开搜索页的方式：导航到本轮关键词的
搜索页，并在该页完成行为阶段。第一页是页面加载时自己请求的那一次响应；之后每次先写翻页停顿，再用
`social_high_risk` 的一次真人分段滚动触发下一页，然后等待这次响应。滚动最多 8 次，每次滚动后等待 8 秒；
首页读体最多等 15 秒。在这段等待内截获不到，就以 `runtime_failed`、`stop_detail=browser_search_response_missing`
结束。不改走浏览器外请求，不猜深页，也不能把“没截获到”解释为来源耗尽；缺失页的候选不写入已处理集合。

搜索页导航、翻页停顿、滚动和 `search_v3` 截获写入访问账本。每条搜索访问带连续 `search_page`（从 1 起）、
`is_target=true`、`has_more`（末页为 false）、`search_session` 和 `filter_signature=type=content`。来源耗尽
停止事件带最后一页的 `source_visit_id`。详情、作者和正文图也都记入同一本账本：详情与作者是页面状态，
正文图是 `cdn_direct`（状态码、资产键、sha256）。知乎 discovery 链是
`search_results → note_detail → creator_profile → images`。

## 发现与诊断

来源耗尽只认 search_v3 原始响应的 `paging.is_end is True`，停止细节为 `paging_is_end`，批次
`source_has_more` 取自响应。过滤后为空（例如整页都是 zvideo 或非内容卡片）但 `is_end=false` 的页
照常完成批次并翻到下一页，`raw_batch_count` 记原始条数。响应缺 `paging` 或 `data`、`data` 为空却未
声明 `is_end=true`、HTTP 404 都以 `runtime_failed/search_response_invalid` 结束，不推断为耗尽。
2026-10-30 之前按“过滤后空页”记下的 `empty_page` 耗尽结论不可信，需重新扫描。

每轮从搜索第一页滚到 `is_end=true`（#185）。不按保存页码恢复深层前沿，也不做顶部有限刷新。数据库已有
ID、累计摘要中的有效 ID 与持久已处理候选在详情和图片请求前滑过。runner 不把 checkpoint 转成
`--start-page`、`--top-refresh-max-pages` 或 `--discovery-source-exhausted`；child 收到这些参数直接以
`zhihu_full_scan_rejects_resume_args` 报错。运行级失败时的未完成累计摘要仍经 `--resume-summary` 合并，
下一轮取得 `is_end=true` 后一次性入库。checkpoint 仍写入本轮最后页码与停止原因供审计，不作续跑起点。

批次使用 `stagnation_basis=valid_new` 记录诊断计数：只有批次没有新增满足正式字段 profile、且
数据库中不存在的有效记录时才累计；新无效候选、重复候选和数据库已有记录都不能重置该计数。
该计数不触发停止，正式完成只接受来源耗尽。

重新核验一组已知 answer/article URL 时，使用根执行器的
`--zhihu-detail-urls-file <JSON数组文件> --no-import` 诊断模式。它不写正式 checkpoint 或数据库，
也不能作为正式轮次完成证据；不要绕过根执行器直接运行 worker 入口。

需要正式修复历史库中非 `detail_observed` 的既有行时，改用
`scripts/repair_post_details.py --platform zhihu`。总控清单把既有内容 ID 与 answer/article HTTPS URL
严格绑定，detail 结果仍必须取得 `answer_detail` 或 `article_detail`、作者页粉丝证据和完整正文图，
再走媒体晋升与 SQLite 事务。知乎 detail 页缺少作者粉丝或 answer 创建时间时，入口会从同一旧行
保留已观察的 `search_author` 粉丝证据和 `published_at`，只补齐详情载荷缺失的元数据，不替换详情正文。
详情解析必须在页面结构化 JSON 中按请求 ID 精确选取非空 answer/article entity，不能取实体字典中的
第一项或把标题、空对象标为已观察。单帖正文图下载失败只跳过该候选并继续同批后续目标；只有授权、
频控和登录等运行级错误才中止批次。该修复入口不写搜索 checkpoint，完成含义只覆盖清单中的旧行。
