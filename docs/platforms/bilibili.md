# B站 article

> **修复状态（2026-08-07）：** 新抓取路径已改为先发现 article ID，再取得详情正文和正文图片；
> 153 项项目测试及真实 `--no-import` 小样通过。历史冻结范围 3,009 条已完成处理：3,006 条详情成功
> 并原位更新，另有 2 条 `operator_excluded` 和 1 条 `invalid_detail`。用户随后明确要求从当前业务
> 数据删除这 3 条未成功修复记录；当前默认库保留 3,006 条 B站记录，全部具有详情观察证据。历史
> sidecar、原始 artifact、报告和修复前备份继续作为审计/恢复证据。全库校验及 5 条分层实时复取
> 验收均通过。
> 事件证据见
> [`2026-08-02-bilibili-article-completeness.md`](../incidents/2026-08-02-bilibili-article-completeness.md)，
> 全库回填步骤见
> [`2026-08-02-bilibili-full-library-repair.md`](../plans/2026-08-02-bilibili-full-library-repair.md)。

## 入口与范围

- 正式入口：`crawl_runner.py` 调用 `mediacrawler_crawl.py --platforms bilibili`。
- 内容范围：只发现 B站 article/专栏图文，不使用视频搜索。单页 Opus 抓取只用于定向页面证据，
  不代表正式平台轮次。
- 完成模式服从正式契约：正常默认使用 `target-new-posts`；只有用户明确要求某一轮直到来源耗尽时，
  才临时使用 `source-exhausted`，不修改长期配置。
- 配置中的 job kind 虽为 `mediacrawler_search`，B站实际使用项目自有 article API 分支；发现
  checkpoint、候选记忆和跨次累计仍复用通用控制面。

## 正常正式抓取调用链

历史回填完成不代表正常抓取要继续调用回填工具。新关键词或后续定时轮次的权威路径固定为：

1. `config/crawl_targets.json` 中存在 `site_key=bilibili`、`job_kind=mediacrawler_search`、
   `params.platform=bilibili` 且 `enabled=true` 的 job；当前长期 job 为
   `mc_bilibili_qingdao_laoshan_guide_article`。
2. `crawl_runner.py` 同步并选择该 job，构造
   `mediacrawler_crawl.py --platforms bilibili`，同时传入关键词、完成模式、数量边界、字段 profile、
   数据库和自动发现 checkpoint 参数。
3. `mediacrawler_crawl.py` 的平台分派直接进入 `run_bilibili_article_search()`；不会进入第三方
   MediaCrawler 的视频搜索，也不会进入 `repair_bilibili_articles.py`。
4. 每个未知 article 依次执行搜索归一化、`fetch_bilibili_article_detail_with_retry()`、
   `hydrate_bilibili_article_record()` 和作者关系统计补全。详情正文和正文图片在这一步替换空的正式
   正文字段；搜索 `desc` 与预览图只保留为原始证据。
5. `validate_formal_record()` 强制检查 `detail_observed`、`article_view_api`、详情图片状态、作者、
   发布时间、至少一张正文图和 `relation_stat` 粉丝来源；任何详情失败都在入库前停止或判无效。
6. 只有正式完成谓词、行为/策略证据和上述字段门禁同时通过，`import_valid_records()` 才把记录写入
   `web_posts` / `web_post_images`，runner 再完成持久化状态并提交发现记忆。

历史修复入口从 `mediacrawler_crawl.py` 导入同一组详情请求、正文清洗和图片提取函数；因此代码所有权
在正常正式执行器，而不是 sidecar 修复脚本。以后不得复制第二套 B站解析器，也不得为了新抓取恢复
旧的“标题/搜索摘要作为正文”分支。

只验证冻结计划、不访问平台时使用：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --job-key mc_bilibili_qingdao_laoshan_guide_article \
  --completion-mode target-new-posts
```

计划中的 child 命令必须包含 `--platforms bilibili`、正式字段 profile 和本轮完成模式；缺少任何一项
都不能进入正式运行。dry-run 本身会同步调度表并写计划/冻结状态，不是数据库严格只读操作。

对应防回归证据分为三层：`tests/test_bilibili_formal_route.py` 固定检查长期配置存在已启用 B站 job，
并验证 runner 构造的 child 命令；`tests/test_bilibili_article_detail.py` 固定检查摘要不能通过门禁、
Opus/旧 article 正文与图片解析、短文、重试和失败安全前沿；`tests/test_discovery_checkpoints.py` 固定
检查正常 B站轮次会跳过已知 ID、只对未知 ID 请求详情并从保存页继续。修改 B站配置、入口或字段
说明时必须同时运行这三组测试。

## 字段来源与详情门禁

搜索接口只负责发现候选和提供预览字段。搜索结果中的 `desc`、`image_urls` 分别是摘要和预览图，
不能单独证明正文或正文图片完整。未知 article ID 在进入正式有效集合前必须完成详情补全：

- `content_text` 来自已成功观察的 article 详情正文；标题单独保存，不用“标题 + `desc`”冒充正文。
- 正文规范化保留段落换行；不得使用会把全部空白压成单行的摘要清洗方法处理完整正文。
- 正文图片从详情响应或详情页正文结构提取并去重。搜索 `image_urls` 可作为预览来源留在原始证据，
  但不能作为“已经检查全部正文图片”的唯一证据。
- 当前 Opus 详情优先使用 `opus.content.paragraphs[].pic.pics`；旧 article 优先解析详情正文 HTML，
  仅在没有这些更明确的正文图片结构时才使用详情响应的 `origin_image_urls` / `image_urls`。这是因为
  新 Opus 的同名字段可能只是封面，而旧 article 的同名字段才是正文图。
- 详情成功必须保存 `content_detail_status=detail_observed` 和明确的详情来源；详情未请求、请求失败、
  限流或解析失败均不能进入正式有效集合。
- 粉丝来源仍为作者 `mid` 的关系统计接口，保存数值、`followers_observed=true` 和
  `author_followers_source=relation_stat`。
- 发布时间、赞、评、浏览等字段可以由搜索结果发现，详情响应有更明确值时使用详情值；不得用
  抓取时间补原始发布时间。
- 去重键为 article 内容 ID。

最终记录仍须满足 `image_post_with_followers_v1`，并额外满足上述 B站详情门禁。详情已观察但
文章确实已删除、私密或没有满足字段 profile 的正文图片时，该候选可以作为有决定性证据的无效项；
普通请求失败不能伪装成这种永久无效状态。

## 正文图片本地化

B站使用项目自有 article 分支下载，不调用 MediaCrawler 视频媒体实现。只有
`content_images_detail_status=detail_observed` 的详情 `image_urls` 会生成候选；候选按正文顺序去重，
`source_key=image_urls`、`image_role=content`。BFS 路径移除变换后缀后生成稳定
`source_asset_key`，因此 HTTP/HTTPS、协议相对地址或 CDN 变换 URL 不会制造重复资产。

每张图复用行为阶段导出的当前 B站 Cookie 和 article Referer，空响应或超时按单图最多 3 次、
1–2 秒随机基数指数退避重试后写到本轮
`<platform_data_root>/images/<post_id>/<index>.<real_ext>`；同目录
`image_manifest.jsonl` 原子记录 URL、稳定键、尝试次数、HTTP 状态、SHA、真实 MIME、尺寸和相对
staging 路径。搜索 `image_urls`、Opus 封面、作者头像和视频资源不会进入下载函数。失败行只写
manifest 错误，不留下成功元数据；`image_download_retryable` 与终态图片错误都记录
`candidate_deferred` 后暂时跳过该 article，继续处理后续候选，终态错误不补做无意义重试。失败 ID
仅在当前 child 内抑制重复请求，不写持久候选记忆；最终
checkpoint 回到最早失败页并保持 `last_batch_complete=false`。
若同一 article 跨轮完成有限重试后仍持续失败，只有操作人明确批准该精确 job、查询指纹和 article
ID 排除时，才写入 `crawl_discovery_candidate_exclusions`。下一轮仍从原页恢复，但在详情请求前跳过
该 ID 并继续处理同页后续候选；不得自动按失败次数排除、写 seen、手工推进页码或把排除计为成功。

根执行器按同一详情投影逐项核对 manifest 和字节。正式运行才把文件晋升到
`data/media/bilibili/...` 并在同一帖子事务写入 `web_post_images.local_path` 等字段；
`--no-import --download-images --media-root temp/<目录>` 只验证 staging，不改长期目录或数据库。
任一图片不完整时 `image_materialization.complete=false`，不能靠正文和 URL 通过正式门禁。
格式、解码、大小和明确非重试 HTTP 等终态图片错误写失败 manifest 后以 `runtime_failed` 停在当前
页，不写 deferred 或 seen；重试、恢复和最终失败均写短日志事件。
HTTP 200 空 body 也按空响应重试；明确非重试 HTTP 保留状态码。响应头或流式字节超过统一上限时
错误码固定为 `image_too_large`。

## 请求节奏、失败与安全前沿

article 搜索前必须在 MediaCrawler 持久 profile 执行共享行为阶段，并让搜索、详情和关系统计请求
复用同一会话 cookie。行为与请求策略证据缺失时不得入库。

详情请求必须串行或按经验证的低并发执行，并记录随机等待、重试次数和业务状态。`-509`、HTTP
失败、超时和无法解析的详情属于可恢复运行错误：允许有限退避重试；仍失败时停止当前 child，当前
页标记 `last_batch_complete=false`，恢复页保持为本次请求页。失败 article ID 不得写入
`crawl_discovery_seen_candidates`，也不得因搜索摘要非空而进入累计摘要。
操作人另行授权的精确排除只写独立排除表，不改变上述失败默认语义。

无人值守历史修复不能只凭 Cookie 文件存在判断已登录。每个修复批次开始和会话间隔结束后，必须用
同一 Cookie 调用 `/x/web-interface/nav`，只有 `code=0` 且 `data.isLogin=true` 才能继续；快照缺失、
接口返回 `-101`、`isLogin=false`，或 article 详情中途返回 `-101` 时立即停止，保留当前记录原状态，
不得把它记为无效正文或继续请求。登录校验本身无法取得决定性结果时也停止无人值守任务，等待人工
复验。

持久化 profile 可能在人工登录后自动恢复有效 Cookie，预热程序确认成功便会退出，因此“不停留登录
页”不能作为未登录证据；必须以上述实时接口结果为准。禁止为了显示登录页而主动登出或清除 profile。

只有以下候选才算“已完成处理”，可以进入持久候选记忆：

- 详情和正式字段全部成功，成为有效记录；
- 详情已获得决定性证据，但内容类型或字段永久不符合正式 profile；
- 平台明确证明 article 已删除、私密或不存在。

首次运行从第 1 页开始；有 checkpoint 时先刷新 `top_refresh_max_pages` 个顶部页，再从
`resume_page` 继续。数据库、累计摘要、已完成候选记忆和本轮已完成集合中的 ID 在昂贵详情前跳过。
每个完整深层页保存下一页；目标或候选上限在页中触发时保留当前页，下一轮重取边界并靠已完成
候选去重。空页才保存 `status=exhausted`。默认数量模式未达标不单独入库；显式来源耗尽模式也
必须先满足全部详情门禁，不能用搜索来源耗尽替代详情成功。

B站停滞仍按是否发现未知 article ID 判断，但“未知”不等于“完成处理”：可恢复详情失败必须停止并
保留安全前沿，不能用它重置停滞后继续跨页。判断是否扩容时同时检查新 ID、详情成功数和有效新增数。

## 历史摘要记录回填

正常正式抓取会在详情前跳过数据库已有 ID，因此不能修复既有摘要记录。历史修复必须使用独立、
可恢复的 B站详情回填入口，并遵守以下边界：

- 写默认库前创建 SQLite 一致性备份，先在临时库验证正文、图片和 upsert 行数。
- 以数据库现有 B站 `platform_post_id` 为输入，不清空或改写搜索 checkpoint、候选记忆和任务状态。
- 详情成功后只 upsert 同一平台 ID，不新增重复主表行；保留原关键词并写入新的详情来源证据。
- 限流或请求失败的 ID 留在回填待处理状态，允许下次恢复；不得把摘要原文重新标记为完整。
- 分别报告计划数、详情成功数、更新数、永久无效数、待重试数和解析错误，并核对正文图片关系。
- 固定 100 条临时演练与全量回填使用独立 supervisor 串联：百条范围必须同时满足 `pending=0`、
  `retryable=0` 且逐条校验通过，才增量回写成功项并克隆 sidecar 到默认库；限流项必须先在百条阶段
  重试成功或取得永久终态，不能带着 `retryable` 提前切换全量。
- supervisor 可以从全量 sidecar 恢复，但登录失效或登录状态无法验证时必须非零退出，不能自动重启；
  只有全量 `pending=0`、`retryable=0` 且数据库不变量通过后才能报告完成。
- 只有用户明确逐项批准放弃重试时，才可用受控入口把指定 `retryable` 转为独立终态
  `operator_excluded`。该动作必须记录原因和事件、保留目标库原正文/图片且通过前后逐条哈希及外部
  不变量校验；不得把人工排除计入 `succeeded`，也不得用 `invalid_detail` 混淆两类原因。

临时候选预算仍按运行手册使用 one-off 配置；它是正常搜索 child 的未知候选预算，不是历史详情
回填预算，也不能替代专门回填。
