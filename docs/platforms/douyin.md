# 抖音

抖音正式抓取服从[正式抓取执行契约](../formal-crawl-contract.md)。本文只维护浏览器响应截获、
每轮全量翻页、详情和严格图文资产差异。

## 入口与字段

- 正式入口：`crawl_runner.py` 经 `mediacrawler_crawl.py` 启动根包 worker（`trippostcollect.platforms.entry`）
  执行抖音搜索，实现位于 `src/trippostcollect/platforms/douyin/`。
- 登录后先进入本轮关键词页，再执行共享行为阶段并刷新 API Cookie。
- 只接受图文作品（`aweme_type=68` 或带正文 `images`）；视频计入已处理候选，不请求作者资料，
  不写内容记录，也不下载媒体。内容记录不写 `cover_url`、`video_download_url`、`music_download_url`。
- 正文来源如实标注，不为每篇额外请求详情：搜索路径直接使用搜索卡片 `aweme_info`，保存
  `content_detail_source=search_aweme_info`；只有经过 `/aweme/v1/web/aweme/detail/`、浏览器详情响应
  或页面结构化状态的 detail/repair 路径才保存 `content_detail_source=aweme_detail`。
- 两种来源都只有在 `aweme_id` 与候选完全一致、是图文、`desc` 非空、带 `create_time` 和 `author.uid`
  时才写 `content_detail_status=detail_observed`，否则写 `detail_unobserved`；`desc` 为空时不用标题或
  封面文字顶替。
- 正文图片来自 `note_download_url`；`images[].uri` 形成 `douyin:uri:<uri>` 稳定资产键。
- 作者粉丝只读作者资料响应（`/aweme/v1/web/user/profile/other/`）`user` 节点的 `follower_count`，
  缺失时读 `mplatform_followers_count`。只有资料响应 `status_code=0` 且带 `user` 节点、来源为
  `creator_profile` 时才 `followers_observed=true`；搜索作者对象里的粉丝数（含占位 0）一律不算已观测。
- worker 判定有效与导入门禁一致：没有 `creator_profile` 粉丝、正文证据或正文图的候选不算有效，
  也不下载正文图。
- 作者补全覆盖本轮全部未知图文候选，不使用固定作者数量上限。
- 作者平台 ID 保存 `author.uid` 原始值（写在 `creator_hash` 键），昵称保存 `author.nickname` 原文；
  `sec_uid`、`short_id`、`unique_id`、签名、头像和 IP 归属地不写出。
- 去重键为 `aweme_id`。

## 搜索响应与游标

搜索只消费浏览器在本轮搜索页上自身发出的请求的响应（#124）。API 客户端在进入关键词页前开始监听
`/aweme/v1/web/general/search/`：首页 offset 0 是进入搜索页时页面发出的 `/stream/` 响应，后续页由
有头搜索页的低频可见滚轮触发页面自身的 `/single/` 请求。同一关键词、offset 和 search ID 有多条响应时，
合并去重并优先最新的非 `verify_check` 响应；全部为验证信号时按风控失败。

连续翻页时目标只比已截获的页深一屏。目标 offset 未进入截获缓存时，最多执行 8 次可见滚轮；仍未截获
即以 `runtime_failed`、`stop_detail=browser_search_response_missing` 停止。首页截获不到同样是
`browser_search_response_missing`。不回退到浏览器外的 `/single/`、`/stream/` 直接请求，不猜深页，
不用可见卡片 ID 加详情请求重建首页，缺失页的候选不写入已处理集合；不能把“浏览器尚未加载”解释为耗尽。

搜索接口契约（页面自身请求的形状，客户端只校验响应）：

- 首页为 `/general/search/stream/`，后续为 `/general/search/single/`，offset 按 10 递增；
- 首页流可能保留 HTTP chunk 边界，解析前必须重组全部 chunk；
- 首页响应的 `extra.logid` 作为本轮 search ID，后续页始终复用它，不把每页轮换的 `logid` 当作新 cursor。

非成功 `status_code`、`data` 非列表、`has_more` 缺失/非法，或 `has_more=true` 但没有下一 `logid`，
均为 `runtime_failed`。`SearchResponseError` 的具体 reason 必须进入停止证据；状态只保存脱敏元数据，
不保存完整响应。`search_nil_info.search_nil_type=verify_check` 固定为 `search_verify_check`。
响应带 `extra: null` 时按空 `logid` 处理。

## HTTP 状态与请求头

作者资料与详情 API 响应在解码前先按 HTTP 状态分类，阻断类不进重试，也不改走浏览器详情：

- 401：`login_required`；
- 403 且响应体含 `ArgusSecurityPlugin` 或 `Blocked by`：边缘网关拦截，`blocked_or_forbidden`；
- 429：`rate_limited`；
- 其他非 2xx：可重试的传输失败，作者资料按“作者补全失败”有限重试后记为候选跳过。

作者资料遇到前三类时，停止证据为 `runtime_failed`，`stop_detail` 为对应错误码，checkpoint 记录
当前 page、offset 和 search ID 供审计。作者资料响应 `status_code` 非 0 或缺 `user` 节点时视为空资料，
走 `creator_profile_empty` 候选跳过。

只有 `/aweme/v1/web/aweme/detail/` 请求带 `x-tt-argus: 1`（ArgusSecurityPlugin 当前不校验取值）；
作者资料请求不加，因为真实浏览器在该请求上的取值不是 1。签名参数 `uifid` 优先取 `UIFID`
Cookie，缺失时才退到 `UIFID_TEMP`。正文图请求带与 API 同源的浏览器 `User-Agent`、
`Referer: https://www.douyin.com/` 和图片 `Accept`，不带 Cookie。

## 空首页门禁

新鲜 page 1、offset 0、空 search ID 返回 `data=[]、has_more=false` 时，必须核对当前可见页面：

- 存在作品链接、`.search-result-card` 或 `waterfall_item_*`：
  `empty_api_response_with_visible_results`；
- 没有卡片，也没有明确可见“无结果”：`ambiguous_empty_first_page`；
- 只有页面明确显示无结果：`verified_empty_first_page`，可以证明当前链耗尽。

前两项属于运行失败；下一轮照常从首页重新扫描。

## 每轮全量扫描与 checkpoint

抖音与小红书一样，每轮从第 1 页、offset 0、空 search ID 连续翻到平台 `has_more=false`（#124）。
search ID 标识一次具体排序，新进程打开搜索页得到新 search ID，旧 offset 在新链上没有意义，页面也滚不到
那么深；因此不跨进程跳到深层 page/offset/search ID，也没有顶部刷新、前沿重绑定或耗尽后 reseed。
数据库已有 ID、累计摘要中的有效 ID 与持久已处理候选在作者、详情与媒体请求前滑过，不会重复入库。

runner 不把 checkpoint 转成 `--start-page`、`--top-refresh-max-pages` 或
`--discovery-source-exhausted`；child 收到这些参数直接以 `douyin_full_scan_rejects_resume_args` 报错，
`--start-offset`、`--start-cursor` 已删除。运行级失败时的未完成累计摘要仍经 `--resume-summary` 合并，
下一轮取得 `has_more=false` 后一次性入库。

checkpoint 仍写入本轮最后的 page、offset、search ID 与停止原因供审计，不作续跑起点。完整批次记录
下一 page、`offset + 10` 和本轮 search ID；页中途停止记录当前三元组。`data=[]` 且 `has_more=true` 是可
继续空批次；`has_more=false` 结束本轮（`stop_detail=has_more_false`），新鲜首页仍必须满足上面的可见
无结果门禁。checkpoint 为 `exhausted` 只是上一轮的真实证据，下一轮仍从第 1 页全量扫描，不退化为只刷顶部。

批次继续使用 `stagnation_basis=valid_new` 记录诊断计数：只有批次没有新增满足正式字段 profile、且
数据库中不存在的有效记录时才累计；新无效候选、重复候选和数据库已有记录都不能重置该计数。
无论计数多高都继续分页，不能触发停止或代替来源耗尽。

## 严格 images-only

只有确认的图文作品进入图片入口。封面、视频、音乐、头像和搜索预览地址既不写入内容记录，也不进入
`ImageCandidate`、staging 或 manifest；代码不得调用 `get_aweme_video()`、视频 store 或音乐下载。平台层使用当前 `dy_client`
会话和新鲜签名 URL 写 staging/manifest，根项目按 `note_download_url` 重建候选并执行通用字节复验、
同帖去重、晋升和 SQLite 事务。

2026-10 之前入库的搜索路径记录仍标为 `content_detail_source=aweme_detail`，实际正文来自搜索卡片；
是否改写这批历史标签登记在 #117，不在本平台代码中处理。

历史库中缺少 `content_detail_status=detail_observed` 的既有图文记录，从根项目
`scripts/repair_post_details.py --platform douyin` 分批修复。清单只接受与既有 `aweme_id` 完全匹配的
`/video/<id>` 或 `/note/<id>` HTTPS 入口；detail 模式先在本轮青岛关键词搜索页完成人类行为证据，
再请求 `aweme_detail`、作者主页和正文图。HTTP detail 返回空壳时，修复入口依次打开目标
`/note/<id>` 与 `/video/<id>`，只接受浏览器详情响应或页面结构化状态中 ID 完全一致、且含详情形状
字段的 aweme 对象；ID-only 路由状态和搜索卡片仍不能补签。该入口不写搜索 checkpoint，完成含义只
覆盖清单中的旧行。

共享的有限重试、`candidate_skipped`、运行级阻断和媒体失败语义见
[正式契约](../formal-crawl-contract.md)。视频、音乐和封面文件计数必须始终为 0，正文图不完整时不能
URL-only 完成。
