# 抖音

抖音正式抓取服从[正式抓取执行契约](../formal-crawl-contract.md)。本文只维护浏览器响应监听、
offset/search ID、详情和严格图文资产差异。

## 入口与字段

- 正式入口：`crawl_runner.py` 经 `mediacrawler_crawl.py` 启动根包 worker（`trippostcollect.platforms.entry`）
  执行抖音搜索，实现位于 `src/trippostcollect/platforms/douyin/`。
- 登录后先进入本轮关键词页，再执行共享行为阶段并刷新 API Cookie。
- 只接受图文作品；视频计入已处理候选但不进入内容或媒体下载。
- 正文只接受 aweme 详情非空 `desc`，保存
  `content_detail_status=detail_observed`、`content_detail_source=aweme_detail`。
- 正文图片来自 `note_download_url`；`images[].uri` 形成 `douyin:uri:<uri>` 稳定资产键。
- 作者粉丝来自图文作者主页，要求 `followers_observed=true`；搜索作者对象的占位 0 不能通过。
- 作者补全覆盖本轮全部未知图文候选，不使用固定作者数量上限。
- 作者平台 ID 保存 `author.uid` 原始值（写在 `creator_hash` 键），昵称保存 `author.nickname` 原文；
  `sec_uid`、`short_id`、`unique_id`、签名、头像和 IP 归属地不写出。
- 去重键为 `aweme_id`。

## 搜索响应与游标

API 客户端在进入关键词页前开始监听浏览器搜索响应。搜索优先消费浏览器已取得的首页和滚动分页，
只有超过可见前沿时才发后续请求。同一关键词、offset 和 search ID 有多条响应时，合并去重并优先
最新的非 `verify_check` 响应；全部为验证信号时按风控失败。

目标 offset 未进入监听缓存时，先在有头搜索页最多执行 8 次低频可见滚轮。仍未取得目标响应才回退
到 API 请求，不能把“浏览器尚未加载”解释为耗尽。首页流无法读取或额外首页请求返回验证时，只有
页面至少有 10 个可见 `waterfall_item_*` 卡片，并且浏览器已取得 offset 10 的健康响应和非空 search
ID，才允许用前 10 个可见作品 ID 的详情重建首页；证据不足必须停止。

搜索接口契约：

- 新鲜首页使用 `/general/search/stream/`，后续使用 `/general/search/single/`；
- `count=10`、`list_type=single`、`search_source=normal_search`，offset 按 10 递增；
- 首页流可能保留 HTTP chunk 边界，解析前必须重组全部 chunk；
- 首页可由浏览器 offset 10 请求携带的 search ID 建链，也可在健康首页流中使用 `extra.logid`；
- 后续页始终复用已经建立的 search ID，不把每页轮换 `logid` 当作新 cursor；
- 禁止旧 `from_group_id` 或 `count=15/list_type=multi` 组合。

非成功 `status_code`、`data` 非列表、`has_more` 缺失/非法，或 `has_more=true` 但没有下一 `logid`，
均为 `runtime_failed`。`SearchResponseError` 的具体 reason 必须进入停止证据；状态只保存脱敏元数据，
不保存完整响应。`search_nil_info.search_nil_type=verify_check` 固定为 `search_verify_check`。

## 空首页门禁

新鲜 page 1、offset 0、空 search ID 返回 `data=[]、has_more=false` 时，必须核对当前可见页面：

- 存在作品链接、`.search-result-card` 或 `waterfall_item_*`：
  `empty_api_response_with_visible_results`；
- 没有卡片，也没有明确可见“无结果”：`ambiguous_empty_first_page`；
- 只有页面明确显示无结果：`verified_empty_first_page`，可以证明当前链耗尽。

前两项属于运行失败，checkpoint 保持 page 1、offset 0、空 search ID。

## checkpoint 与重新建链

checkpoint 必须成组保存下一 page、offset 和稳定 search ID。深层恢复缺任一项都直接拒绝。旧摘要
误存轮换 log ID 时，runner 只允许从冻结摘要首个 frontier batch 的建链证据纠正，并在 dry-run 写
`resume_cursor_corrected_from_summary=true`。

有 checkpoint 时先用空 cursor 刷新顶部。search ID 不能跨进程照搬：只有本轮刷新取得健康后页、
`has_more=true` 和非空稳定 ID，才用新 ID 重新绑定深层前沿，并写
`douyin_frontier_cursor_rebound`。刷新未覆盖旧 offset 时保留原 page/offset；已经覆盖时从刷新后的
下一 offset 继续。没有健康刷新链时保留旧 checkpoint 并失败。

耗尽状态只证明旧 search ID 链结束。顶部刷新同时发现未知 `aweme_id`、`has_more=true` 和非空稳定
search ID 时，从刷新链下一页写 `discovery_frontier_reseeded` 并替换旧前沿；否则保持耗尽。

完整响应保存下一 page、`offset + 10` 和稳定 ID；页中途停止保存当前三元组。`data=[]` 且
`has_more=true` 是可继续空批次；深层 `has_more=false` 可以结束当前链，但新鲜首页仍必须满足上面的
可见无结果门禁。

批次继续使用 `stagnation_basis=valid_new` 记录诊断计数：只有批次没有新增满足正式字段 profile、且
数据库中不存在的有效记录时才累计；新无效候选、重复候选和数据库已有记录都不能重置该计数。
无论计数多高都继续分页，不能触发停止或代替来源耗尽。

## 严格 images-only

只有确认的图文作品进入图片入口。`cover_url`、封面、`video_download_url`、音乐、头像和搜索预览
均不进入 `ImageCandidate`、staging 或 manifest；代码不得调用 `get_aweme_video()`、视频 store 或音乐下载。平台层使用当前 `dy_client`
会话和新鲜签名 URL 写 staging/manifest，根项目按 `note_download_url` 重建候选并执行通用字节复验、
同帖去重、晋升和 SQLite 事务。

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
