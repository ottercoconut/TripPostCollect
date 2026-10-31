# B站 article

B站正式能力只抓取 article/专栏图文。来源耗尽、候选失败、图片重试、事务和 checkpoint 的共享语义
见[正式抓取执行契约](../formal-crawl-contract.md)；本文只记录 B站差异。

## 入口

- 正式入口：`crawl_runner.py` 调用 `mediacrawler_crawl.py --platforms bilibili`。
- 配置仍使用 `job_kind=mediacrawler_search`（历史沿用的值），实际在中间层进程内分派到项目自有
  `run_bilibili_article_search()`，不启动 worker，也不进入视频搜索或历史修复入口。
- 单页 Opus 页面证据不代表 B站正式平台轮次。
- 发现 checkpoint、seen、人工排除和跨次累计复用通用控制面。

只冻结当前长期 B站 job：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --job-key mc_bilibili_qingdao_laoshan_guide_article
```

计划必须包含 `--platforms bilibili`、正式字段 profile、正文图片参数和来源耗尽策略。

## 详情与字段来源

搜索接口只负责发现 article ID 和保存预览证据。搜索 `desc`、标题与预览 `image_urls` 不能作为完整
正文或正文图片。每个未知 ID 必须执行 article 详情和作者关系统计补全：

- 正文来自成功观察的 article/Opus 详情，保存
  `content_detail_status=detail_observed`、`content_detail_source=article_view_api`。
- 详情 `content` 有两种格式：旧编辑器是 HTML；新编辑器（`template_id=4`）是 Quill delta JSON
  `{"ops":[{"insert":...}]}`。delta 正文由 `bilibili_delta_to_text()` 按 ops 拼接：字符串 `insert` 原样
  取文本，对象 `insert`（`native-image` 图片、`cut-off` 分割线、卡片）不进入文本。拼不出任何文本时
  按“详情无可解析正文”处理，有限重试后形成 `candidate_skipped(failure_scope=post)`。
- 正文规范化保留段落换行，不使用摘要清洗器把全文压成单行。
- 正文图按 `opus.content.paragraphs[].pic.pics`、详情正文（HTML `<img>` 或 delta `native-image`）、
  `content_pic_list` 顺序合并，按 BFS 资产路径去重：同一张图在 `hdslb.com` / `biliimg.com`、不同
  `@` 变换后缀或扩展名下只取第一次出现的地址。
- 详情响应的 `origin_image_urls` / `image_urls` 是文章封面（banner），不作为正文图，也不作为回退。
  没有正文图的文章形成 `missing_content_image` 无效记录，不影响整轮运行与来源耗尽判定。
- 搜索预览图、Opus 封面、作者头像与视频资源均不属于正文图。
- 作者粉丝来自 `mid` 关系统计，要求数值、`followers_observed=true` 和
  `author_followers_source=relation_stat`。
- 正式搜索记录的发布时间来自搜索响应原始 `pubdate` / `pub_time`；详情补全保留该值，
  不以详情时间覆盖，也不以抓取时间补缺。
- 去重键为 article 内容 ID。

正式记录使用 `image_post_with_followers_v1`。详情已观察且平台明确证明删除、私密或字段永久不符时，
可以形成决定性无效结果；空响应、HTTP 5xx 或解析失败必须先有限重试，耗尽后形成
`candidate_skipped(failure_scope=post)`，不能伪装为永久无效。超时、断连等网络传输失败重试耗尽后是
运行级 `network_unreachable`，见下文“搜索、阻断与安全前沿”。

## 图片资产差异

只有详情观察后的正文 `image_urls` 生成 `ImageCandidate(role=content)`，来源键为 `image_urls`。
BFS/CDN URL 去除已知变换后缀后形成稳定资产键，避免协议或变换参数制造重复资产。图片请求只带
article Referer 与本轮浏览器 UA，不带 B站 Cookie；重定向目标必须是 `*.hdslb.com` 或 `*.biliimg.com`，否则拒绝跟随并
记 `image_source_unavailable`。

平台层写本轮 staging 与 `image_manifest.jsonl`；根项目负责字节复验、同帖 SHA-256 去重、晋升和
SQLite 事务。共享错误码和重试规则不在本文重复，见[正式契约](../formal-crawl-contract.md)与
[数据持久化](../data-persistence.md)。

## 搜索、阻断与安全前沿

article 搜索前必须在持久 profile 完成共享行为阶段，并让 nav、搜索、详情和关系统计请求复用同一会话
Cookie。行为会话与 warmup 共用同一套持久 context 启动配置（沙箱开启、视口 1280×900、`zh-CN` 与
`Asia/Shanghai`，#136），不再单设 1440×900 视口与 `screen`。这些请求与图片请求的 `User-Agent` 都取自行为会话浏览器在关闭前读到的 `navigator.userAgent`，
不写死版本串；拿不到 UA 时在发出任何请求前以 `runtime_failed/cookie_export_missing_user_agent` 停止（#127）。
`scripts/repair_bilibili_articles.py` 不开浏览器，从 warmup 快照读取 Cookie 与 `user_agent`；快照缺
`user_agent`（#127 前写出的旧快照）时以 `login_required`（错误 `cookie_snapshot_missing_user_agent`）
停止，不发 nav 或详情请求，先重跑 `scripts/login_warmup.py --targets bilibili`。

登录态以 nav 响应 `data.isLogin=true` 为准；Cookie 中存在 `SESSDATA` / `DedeUserID` 不能证明
已登录（过期会话的 Cookie 仍留在浏览器里）。nav 未返回 `isLogin=true` 时以
`login_required/bilibili_nav_login_required` 停止，不发搜索请求。

HTTP 401/403/412/429、业务码 `-101/-509/-412/-352`、登录、授权、频控或风控属于运行级阻断：命中即停，
不再重试；保留当前页且不把该 ID 写入 seen。搜索响应 `data.v_voucher` 是风控验证凭据，以
`captcha_detected/bilibili_article_search_risk_control:v_voucher` 停止。nav、搜索、详情与关系统计共用
同一错误分类：HTTP 5xx、超时、断连与非法 JSON 有限重试（3 次）。重试用尽后仍是网络传输失败
（DNS、连接失败、超时、连接重置）是网络整体故障，与候选无关（#129）：nav、搜索、详情与关系统计都按
`runtime_failed:bilibili_<stage>_failed:network_unreachable` 停止，不写 `candidate_skipped`、不进 seen；
runner 记为 `network_timeout` 并等待 900 秒重试。非法 JSON 不是网络故障：nav/搜索按
`bilibili_<stage>_failed:parse` 停止，详情/关系统计仍形成候选跳过并保留错误码。
同一作者的关系统计失败不进缓存，下一篇同作者候选重新请求。正文图片下载不在此列，仍记
`image_download_retryable`。

来源耗尽只接受平台总数证据：首页 `numResults=0`（`stop_detail=no_results`），或当前页码大于
`numPages` 且结果为空（`stop_detail=empty_page`）；停止事件同时记录 `source_num_pages` 与
`source_num_results`。结果为空但页码仍在 `numPages` 内，或响应缺少 `numPages`，属于异常响应，以
`runtime_failed` 停止并保留当前页，不推断为耗尽。

搜索翻页与详情请求使用同一节奏 `BILIBILI_DETAIL_PACING_SECONDS`：第 2 页起每次搜索请求前按区间
随机等待，行为证据记 `request_pacing_events(stage=article_search)`。

首次从第 1 页开始；有 checkpoint 时先刷新顶部，再从 `resume_page` 继续。数据库、累计摘要、seen、
人工排除和本轮已处理集合中的 ID 在详情前跳过。完整深层页保存下一页；页中途停止保留当前页。

批次仍按是否发现未知 article ID 记录停滞诊断，而不是按有效图文数量；该诊断不触发停止。详情或
图片候选级失败形成 `candidate_skipped` 后可作为已处理候选继续跨页，但不增加有效记录数。排障时
同时查看新 ID、详情成功、候选跳过和有效记录数，正式完成仍只接受来源耗尽。

## 历史事件

2026-08-02 发现旧正式分支曾把搜索摘要当完整正文。事件范围、历史数量、校验和最终处置只在
[B站正文完整性事件](../incidents/2026-08-02-bilibili-article-completeness.md)记录。历史回填入口不属于
正常抓取流程，其执行计划从 Git 历史查询。

## 防回归测试

- `tests/test_bilibili_formal_route.py`：配置、runner 路由和 child 命令。
- `tests/test_bilibili_article_detail.py`：摘要拒绝、详情正文/图片、重试和阻断。
- `tests/test_bilibili_issue115.py`：delta 正文与正文图、封面不作正文图、来源耗尽证据、nav 登录态、
  阻断即停、搜索/nav 错误分类、粉丝失败不入缓存、翻页节奏、图片不带 Cookie 与重定向白名单。
- `tests/test_discovery_checkpoints.py`：已知 ID 过滤和安全前沿。
