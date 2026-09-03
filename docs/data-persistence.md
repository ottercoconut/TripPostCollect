# 入库与校验

> **受限冻结：** 本文件属于治理基线；普通抓取、排障或顺手同步不得修改，只有用户明确授权治理变更，并同步核验对应代码、测试与关联文档时才允许更新。

## 数据库文件

默认数据库：

```text
data/trippostcollect.sqlite
```

正式正文图片根目录：

```text
data/media/<platform_key>/<safe_platform_post_id>/<source_index>-<asset_hash>.<real_ext>
```

`web_post_images.local_path` 只保存项目相对路径；对应行还必须保存 `width`、`height`、真实
`mime_type` 和文件 `sha256`。URL 继续作为来源与重试证据保存在 `image_url`，但 URL 本身不再满足
正式图片持久化。`outputs/mediacrawler_runs/.../<platform>/` 或 XHS child artifact 中的图片只是本轮
staging；正式文件只有在根项目复验后才能原子晋升到 `data/media`。同一帖子内复验后 SHA-256
相同的正文图只保留源顺序第一项，重复 URL/资产键/manifest 行写入该保留项
`raw_image_json.local_file.sha256_duplicate_sources`；保留项重新从 0 连续编号。该规则不跨帖子，且
不把视觉近似但字节哈希不同的缩放或转码文件合并。

知乎语义资产归一上线前的恢复摘要可能仍引用同一 `zhimg.com` 资产的 `_r`、`_720w`、`_1440w`
旧 manifest 多行。根项目只在同一个 manifest 内的这些旧行 URL 各不相同、全部下载成功、归一到
同一真实 zhimg 逻辑路径，且每个 staging 文件的路径、元数据和 SHA-256 复验后完全相同时，将其
重验为当前一个候选；每行 `source_asset_key` 仅允许当前逻辑键或仓库历史版本实际生成过的旧算法
键，任意其他键保持失败。成功重验时在 `image_materialization` 写
`legacy_manifest_reconciled_images` 和逐行 `legacy_manifest_reconciliations`。这些旧冗余行不增加当前
`expected_images`；同一 manifest 的路径别名先解析去重，不制造旧变体证据；跨 manifest、同 URL
重复、不同 SHA、外部域名、失败行或跨逻辑资产一律保持 mismatch，等待候选重新抓取。

相关 schema：

| 文件 | 表 |
|---|---|
| `db/source_platforms.sql` | `source_platforms` |
| `db/web_posts.sql` | `web_posts`、`web_post_images`、`post_detail_repair_waivers` |
| `db/ctf_captures.sql` | `ctf_captures`、`ctf_capture_images` |
| `db/crawl_scheduler.sql` | `crawl_jobs`、`crawl_discovery_checkpoints`、`crawl_discovery_seen_candidates`、`crawl_discovery_candidate_exclusions`、`crawl_attempts`、`crawl_run_reports`、`profile_health_checks`；任务类型包含通用搜索和页面证据 |
| `db/xhs_control.sql` | `xhs_accounts`、`xhs_account_events`、`xhs_account_leases`、`xhs_runs`、`xhs_discovery_checkpoints`、`xhs_discovery_seen_candidates` |

`trippostcollect.db.bootstrap` 是统一实现。通用 runner、小红书 runner、MediaCrawler 入库和
CTF artifact 导入都会自动执行 bootstrap，补齐 schema；通用调度任务仍只同步到
`crawl_jobs`，小红书运行与账号状态写入独立 `xhs_*` 表。手工 `--sync-only` 只用于通用
调度配置的显式刷新或排查。

内容入库阶段的 bootstrap 只补齐内容、调度控制面和小红书控制面 schema，不同步
`crawl_jobs`。调度任务只能由父 `crawl_runner.py` 使用本轮冻结配置显式同步；否则父 runner
使用一次性 `--config` 时，child 若按默认主配置同步任务，会删除尚未执行的一次性 job，并在
切换下个平台时触发 attempt 外键失败。

`web_posts` 不再建模城市。迁移 `13/remove_city_name` 使用旧库原有的 `city_name` 完成一次性历史
数据清理，随后移除该列、城市索引和 `cities` 表；迁移 `14/configured_scheduler_scope` 删除不在
当前配置中的历史调度任务。现行项目范围由操作人或 Agent 在计划冻结时核对；配置解析器不按关键词
前缀拒绝启动。“崂山攻略”等不含“青岛”字样但明确属于青岛范围的完整检索词，仍可由正文命中该
完整词而标为相关；没有任何命中时记录仍正常入库但标为不相关。

`crawl_discovery_checkpoints`、`crawl_discovery_seen_candidates` 和
`crawl_discovery_candidate_exclusions` 是 B站、微博、抖音和知乎正式搜索的控制面记忆，不是内容表。
`job_id + query_fingerprint` 唯一定位同一来源查询；`resume_page` 保存下一安全页，抖音同时使用
`resume_offset` 和 `resume_cursor`，`last_stop_reason` 与 `last_stop_detail` 保存停止分类，
`last_summary_path` 指向尚未取得完整来源耗尽证据的累计摘要，
`campaign_candidate_count` 保存累计报告数。`status=exhausted` 表示已保存深层前沿明确耗尽，
后续默认只做顶部刷新；抖音若刷新同时证明存在持久记忆中没有的新候选 ID、`has_more=true` 与可继续的新 search ID，则从刷新链
建立新前沿并把 checkpoint 恢复为 `active`。checkpoint 只能在 child 摘要形成后提交；诊断
`--no-import` 不得更新它。内容仍只在取得可验证来源耗尽证据后写入
`web_posts` / `web_post_images`。通用已完成处理候选表按 job 与查询指纹保存
视频、有决定性证据的字段无效候选、结构有效候选（含主题不相关），以及详情、作者或正文图在适用有限重试后仍失败并
形成 `candidate_skipped` 的候选 ID；它只用于发现去重，不把失败或无效候选变成内容记录。
登录、授权、验证码、安全限制、账号/IP 封禁、频控、搜索请求或浏览器整体故障属于运行级失败，
不得写入该表。只有操作人明确授权某个平台、job、查询指纹和候选 ID
永久跳过时，才把该 ID、原因、证据与授权 run 写入 `crawl_discovery_candidate_exclusions`。正式 child
会在详情、作者和图片等昂贵请求前将它与 seen 一并作为已知 ID 跳过；排除行不创建内容记录、不增加
成功或有效候选计数，也不代替来源末页停止证据。撤销排除必须显式删除对应精确作用域的排除行，
不能清空整张 seen 或 checkpoint 表。

`web_posts` 是统一内容主表，面向用户查询和后续数据使用。`ctf_captures` 是证据和调试底座，面向程序脚本或 Agent 排查抓取过程。页面级抓取成功后，也会归一化生成 `web_posts` 行，并通过 `web_posts.source_capture_id` 关联对应 `ctf_captures.id`。

schema v20 为 `web_posts` 增加失败关闭的
`topic_relevant INTEGER NOT NULL DEFAULT 0 CHECK(topic_relevant IN (0,1))`。所有首方写入显式赋值；
结构完整但不相关的帖子保留在主表供审计，默认用户查询只取 `topic_relevant=1`。

`post_detail_repair_waivers` 是既有帖子历史详情修复的独立操作审计表。它以 `web_post_id` 唯一关联
`web_posts`，只保存用户明确授权的放弃原因、授权主体、授权时间和短证据 JSON。该表不改变
`raw_sample_json.content_detail_status`，不属于 discovery checkpoint、seen 或候选排除，也不把未观察
详情变成有效详情。帖子删除时 waiver 随外键级联删除；普通修复、抓取和导入不得自动创建或撤销 waiver。
schema migration `19/post_detail_repair_waivers` 记录该表已进入现行内容 schema。

当前结构不再保留“只入 `ctf_captures`、不入 `web_posts`”的内容形态。已有成功且内容就绪的页面级证据记录，应通过 `import_ctf_captures.py` 重新导入或同步，使用户查询统一落在 `web_posts` 上。

`xhs_*` 表只保存小红书控制面和审计信息，不替代内容主表：`xhs_accounts` 只保存逻辑槽位 ID、状态及
审计时间；`xhs_account_leases` 防止同一逻辑槽位并发使用，并记录本轮临时 profile 路径及精确 owner/
进程身份；`xhs_account_events`、`xhs_runs` 保存人工状态变化、挑战信号和运行摘要。项目不持久化小红书
Cookie、localStorage、sessionStorage、独立浏览器快照、平台身份哈希或加密密钥；本轮临时 profile
只在租约生命周期内存在，精确收束运行树后删除。`xhs_discovery_checkpoints` 按目标、账号和查询指纹保存下一安全页、
该深层搜索的 `search_id`、耗尽状态、停止原因和未完成累计摘要路径；它不保存 Cookie，也不跨
账号共享未入库活动。`xhs_discovery_seen_candidates` 在相同作用域保存已经完成处理的笔记 ID，
包括因视频或正式字段不足而不进入累计摘要的候选，以及结构有效但主题不相关的候选，避免它们跨轮
反复触发详情和作者请求。小红书全部结构有效图文仍写入 `web_posts` / `web_post_images`。

可捕获的 SIGINT/SIGTERM 不提交 `xhs_discovery_checkpoints` 或 `xhs_discovery_seen_candidates`；runner
必须先把 execution state 与 `xhs_runs` 终结为 `runtime_failed/operator_interrupt`，写顶层
`run_summary.json`，再记录包含精确 `lease_id`、signal 和 `process_check.safe_to_release` 的
`lease_cleanup`。只有 `runtime_session_removed=true`、`lease_released=true` 和
`lease_cleanup.ok=true` 同时成立，才证明该中断轮的控制面与运行树已完整收束。

小红书 checkpoint 与通用表遵守同一提交边界：只有 child `summary.json` 已形成且含分页证据时，
`xhs_runner.py` 才在同一事务提交前沿与已处理候选 ID；来源尚未耗尽时 `last_summary_path` 指向
合并活动的最新摘要，取得完整耗尽证据并成功入库后清空摘要路径但保留深层前沿。
`status=exhausted` 后只刷新顶部；`--no-import` 不更新
checkpoint。`import_result.reason=sqlite_import_failed` 时 runner 和 discovery 提交函数都必须跳过
checkpoint、seen 与 campaign 更新。摘要或其 JSONL 缺失时冻结失败，不能静默丢弃活动。

## 文档边界

抓取入口、命令、完成判据和故障处置统一由[正式抓取执行契约](formal-crawl-contract.md)与
[运行手册](operations-runbook.md)维护。本文件不重复 Agent 执行清单，只定义数据结构、映射、事务和
持久化验证。

## MediaCrawler 结果入库

微博、抖音、知乎等通用结构化结果由 `scripts/mediacrawler_crawl.py` 调用 MediaCrawler 后
导入 `web_posts`；小红书由 `xhs_runner.py` 为人工指定逻辑账号申请互斥租约、创建本轮临时 profile，
再调用同一底层执行器并在该轮唯一 BrowserContext 中完成人工登录。
五个平台都在当前登录/签名会话中把权威正文图下载到本轮 staging，原子生成 schema v1
`image_manifest.jsonl`；根项目按同一显式投影复验 manifest、文件字节和身份。只有来源耗尽、
字段、行为、策略和 staging 图片门禁全部通过，正式运行才晋升到 `data/media` 并注入统一入库映射；
门禁未通过、晋升失败或已确认发生在提交前的 SQLite 导入回滚时，不得留下本轮新建的无引用长期媒体文件。平台显式投影后的全部 manifest 候选均须通过下载与字节复验；
知乎已知 `zhimg` 尺寸 URL 变体在投影时按资源路径合并，不重复生成 manifest。下载后再仅在同帖内
按 SHA-256 保留首次来源并记录重复来源证据。任何图片失败都使该整帖失去正式资格。有限重试耗尽后
的 `image_download_retryable` 与格式、解码、大小或 HTTP 400/404 等候选自身终态错误，都在写完 manifest
与 `candidate_skipped(failure_scope=image)` 后继续其他候选；终态错误保留 `retryable=false` 且不补做
无意义请求。跳过候选不进入结构有效集合，但不会阻断后续真实来源耗尽。
结构有效但主题不相关的记录不是跳过：它照常保存、进入 seen 和 checkpoint，并单独统计。
HTTP 401/403、429 及平台登录、验证码、安全限制、账号/IP 封禁或频控信号必须形成运行级阻断，
不能写 `candidate_skipped`。分页证据或任一 child 表明 `runtime_failed`、登录或验证码阻断时，无论
已经处理或形成多少有效记录，都必须保持 `completion_met=false`，不得晋升或入库；运行失败优先，且不得被图片、行为或策略
门禁的停止原因覆盖。
微博 store 会保留搜索结果中的 `mblog.pics` 图片 URL 和作者粉丝字段；`isLongText=true`
必须用移动详情替换搜索截断文本，失败时不写 JSONL。小红书搜索会补拉
作者主页指标。知乎回答/文章的原始时间、正文图片和作者粉丝会在清洗前保存并归一化；搜索响应
缺图时先请求详情补全，并用 `content_detail_status` 区分详情确认无图和详情未观察；
`zvideo` 记录跳过。B站正式调度用专栏/图文 article 搜索发现候选，但必须再取得 article 详情
正文和正文图片证据；搜索 `desc` 与 `image_urls` 仅为摘要和预览，不能直接入库为完整正文。
所有平台后续 JSONL 中出现的
视频记录均计入 `skipped_video` 并跳过入库。

导入字段映射：

| 目标字段 | 来源字段 |
|---|---|
| `platform_key` | 运行平台：`bilibili`、`xhs`、`weibo`、`douyin`、`zhihu` |
| `platform_post_id` | `note_id`、`aweme_id`、`content_id`、`id` 等非视频内容 ID |
| `canonical_url` | `note_url`、`aweme_url`、`content_url`、`url`、`share_url`，缺失时按平台 ID 拼接 |
| `title` | `title` |
| `content_text` | 仅接受 `content_detail_status=detail_observed` 且 `content_detail_source` 受信任的权威正文：B站 `content_text/content`，微博 `content_text/content`，小红书 `desc`，抖音 `desc`，知乎 `content_text/content`。小红书和知乎可在入库文本中拼接标题与已验证正文，但标题、搜索 `desc/excerpt` 或预览文本不能单独通过 |
| `author_display_name` | `nickname` 或 `user_nickname` |
| `author_platform_id` | 小红书 `user_id`、`creator_hash` 或其他平台用户 ID |
| `author_followers_count` | 微博 `followers_count/fans_count`，小红书作者主页补充字段 `fans_count`、`followers_count` 或 `fans`，知乎搜索结果 `author.follower_count` 归一后的 `followers_count` |
| `published_at` | 发帖时间，统一保存为 Asia/Shanghai ISO 字符串，如 `2024-04-06T15:35:00+08:00`。优先取平台原始发布时间字段，如 `create_time`、`publish_time`、`time`、`datePublished`；`captured_at` 只表示本项目抓取时间 |
| `keyword` | 优先保存每条记录的 `source_keyword`；缺失时回退到最终执行摘要的 `keyword`，即本次 child 命令实际使用的检索词。当前通用结构化 store 会逐条写入 `source_keyword`；自动 checkpoint 延续同一查询词，显式 `--recovery-keyword` 才会产生恢复词。旧记录缺少该字段时，回退值不能作为其原始检索词证据 |
| `topic_relevant` | 使用与 `keyword` 相同的实际检索词，分别检查最终写入 `web_posts.title` 与 `web_posts.content_text` 的字符串。任一字段经 Unicode NFKC、大小写和空白归一后包含“青岛”或完整检索词为 1，否则为 0；字段之间不拼接，平台清洗前文本、raw JSON、作者、URL 或其他元数据均不参与 |
| `post_likes_count` | `liked_count`、知乎 `voteup_count` |
| `post_favorites_count` | `collected_count` 等收藏字段 |
| `post_comments_count` | `comment_count`、`comments_count` 等评论字段 |
| `post_shares_count` | `share_count`、`shared_count` 等分享字段 |
| `post_views_count` | `view_count`、`play_count` 等浏览字段 |
| `web_post_images.image_url`（`content`） | 只来自权威正文图投影：B站详情 `image_urls`、微博 `mblog.pics` 归一后的 `image_list`、小红书详情 `image_list`、抖音图文 `note_download_url`、知乎正文/详情 `image_list`；作者主页、封面、搜索预览、视频、音乐和公式图不进入正文映射 |
| `web_post_images.image_role/image_index` | 固定 `content`；知乎先按 `zhimg` 资源路径合并已知尺寸 URL 变体，投影后的候选全部下载复验，再在同帖内按 SHA-256 保留首次来源并从 0 连续编号 |
| `web_post_images.local_path` | 根项目复验并晋升后的 `data/media/...` 项目相对路径；正式新记录不能为空 |
| `web_post_images.width/height/mime_type/sha256` | 根项目重新读取本地文件得到并与 manifest 相等的字节证据 |
| `web_post_images.raw_image_json`（`content`） | 权威来源字段、`source_asset_key`、manifest 文件/行及 `local_file` 证据；同 SHA 重复来源写入 `local_file.sha256_duplicate_sources`，不混入头像等非正文对象 |
| `raw_sample_json` | MediaCrawler 记录经头像清除后的结构化样本；正式记录必须含 `content_detail_status` 和 `content_detail_source` |

代码在下载前用五个平台显式投影识别正文图，在导入边界识别其他同类字段差异；内部持久化结构
统一写入 `web_posts` / `web_post_images`。视频记录只用于识别和跳过，不进入内容主表。

schema v18 删除 `web_posts.author_avatar_url`，并把 `web_post_images.image_role` 约束为 `content` 或
页面证据使用的 `page`。迁移在单一事务中删除旧头像关系、递归清理所有 schema 声明的 JSON 列，并
按小红书导出结构明确清理序列化 `creator_profile_json.basicInfo.imageb/images` 头像字段；不按 URL
域名、路径或文件名推断头像。随后重建图片表；正文图片、逐帖 `post_images_count` 和非头像作者字段的
数量与哈希必须保持不变。
先用 `scripts/migrate_author_avatar_data.py --dry-run` 在临时副本演练，再去掉 `--dry-run` 迁移正式库；
正式命令会在 `data/backups/author_avatar_removal/` 建一致性备份，并把审计摘要写入
`outputs/database_migrations/<run_id>/author_avatar_removal.json`。既有历史备份与运行产物只报告残留，
不由该命令删除或改写。

schema v20 `topic_relevance` 在单一事务中回填所有历史帖子并建立默认筛选索引；重复执行只报告现有
分布，不重复改写。迁移不改变帖子总数、图片关系、媒体文件、正文或作者字段，并报告总量以及平台、
关键词的相关/不相关分布。先在临时库运行
`python scripts/migrate_topic_relevance.py --db <临时库> --dry-run`；正式迁移必须另行授权，命令会先在
`data/backups/topic_relevance/<run_id>/` 建 SQLite 一致性备份，再提交并把报告写到
`outputs/database_migrations/<run_id>/topic_relevance.json`。

显式使用 `--recovery-keyword` 人工续跑时，最终摘要会合并旧、新两轮记录，
正常记录的 `web_posts.keyword` 会逐条保存真实来源，因此同一个最终 `artifact_dir` 可以同时
出现原关键词和恢复关键词。若旧记录缺少 `source_keyword`，必须结合原摘要和 JSONL 审计，
不能把回退到最终摘要的值解释成原始检索词。

系统不再保存 `city_name`，也不做城市别名解析、分词或正文地理推断；主题标记只按上文的两个完整
字符串包含规则计算。项目范围仍以该轮冻结的任务配置和操作审计为准。
原正式任务意图以该轮冻结 execution state 的 `plan.job_params`、
`plan.command` 和递归 resume 摘要链为准；`crawl_jobs` 当前值只用于核对现行调度配置，不能
单独证明历史轮次意图，也不能仅用内容行的 `keyword` 反推整轮唯一任务关键词。

本次入库完成后应立即按最终 `artifact_dir` 核对行数与 `import_result.processed_rows`，并按
`keyword` 分组报告来源分布，同时检查 `raw_sample_json.source_keyword`。`artifact_dir` 会在
后续 upsert 时更新，不是不可变的历史成员关系；长期审计仍以冻结状态和摘要链为准。不得把
恢复词记录静默改名或误报为关键词错配。

B站还要按本轮 `artifact_dir` 检查 `raw_sample_json.content_detail_status=detail_observed`、详情来源、
详情正文长度和正文图片关系。搜索摘要长度、非空 `content_text` 或 `status=captured` 均不能单独
证明正文完整；详情失败记录不得进入入库集合。

运行命令：

正式调度调用执行器后，摘要同时包含正式校验和入库结果：

```json
{
  "formal_validation": {
    "candidate_count": 80,
    "valid_new_count": 50,
    "valid_existing_count": 15,
    "topic_relevant_new_count": 50,
    "topic_irrelevant_new_count": 6,
    "source_exhausted_met": true,
    "stop_reason": "source_exhausted",
    "image_materialization_complete": true
  },
  "image_materialization": {
    "required": true,
    "promotion_required": true,
    "candidate_posts": 71,
    "complete_posts": 71,
    "expected_images": 240,
    "downloaded_images": 240,
    "validated_images": 240,
    "unique_images": 235,
    "sha256_duplicate_images": 5,
    "sha256_duplicates": [
      {
        "identity": "weibo:id:<id>",
        "sha256": "<sha256>",
        "retained_source_index": 0,
        "duplicate_source_index": 1
      }
    ],
    "promoted_images": 215,
    "reused_images": 20,
    "retryable_failures": 0,
    "terminal_failures": 0,
    "complete": true,
    "manifest_paths": ["outputs/.../weibo/image_manifest.jsonl"],
    "manifest_sha256": "<aggregate-sha256>",
    "manifest_evidence": [{"path": "outputs/.../weibo/image_manifest.jsonl", "sha256": "<sha256>"}],
    "failures": []
  },
  "import_result": {
    "db": "data/trippostcollect.sqlite",
    "processed_rows": 71,
    "inserted_rows": 56,
    "updated_rows": 15,
    "topic_relevant_inserted_rows": 50,
    "topic_relevant_updated_rows": 15,
    "topic_irrelevant_inserted_rows": 6,
    "topic_irrelevant_updated_rows": 0
  }
}
```

## 页面级抓取结果入库

B站 Opus 详情页由 `ctf_resource_crawl.py` 保存页面文本、HTML、截图和请求聚合证据，再由
`import_ctf_captures.py` 写入 `ctf_captures` 与 `web_posts`。页面级抓取会从
`article:published_time`、JSON-LD、`time[datetime]` 等明确页面元数据中提取 `published_at`，并在
抓取元数据中直接保存为 Asia/Shanghai ISO 字符串；B站 Opus/图文页还会从可见文本中的明确日期行
提取。没有明确证据时保持为空，不用抓取时间替代。截图属于整页证据附件，截图失败会记录到
`artifact_errors`，但页面内容和文本已成功采集时不单独把内容标成失败。

页面证据没有可证明的正文图片角色，因此不读取或保存任意图片响应 URL/响应体，图片请求只保留
非识别聚合计数，`images.json` 和 `failed_images.json` 固定为空数组。导入新旧产物时均不创建
`ctf_capture_images` 或 `web_post_images.image_role=page`，并删除同一 capture/post 的既有未分类
图片行；截图不拆分为图片关系。页面元数据仍须在 JSON/SQLite 序列化前执行共享头像清除器。

当前 `config/crawl_targets.json` 没有页面证据正式任务，以下直接命令只用于开发或诊断验证。
该执行器使用独立浏览器 profile，`login_warmup.py --targets all` 不验证它。若以后新增固定 URL
正式任务，应配置 `job_kind=ctf_resource_crawl` 并通过 `crawl_runner.py` 执行和自动导入。

单次抓取：

```bash
source .venv/bin/activate
python scripts/ctf_resource_crawl.py \
  --sites bilibili \
  --keyword 崂山攻略 \
  --headless \
  --max-scrolls 2
```

导入指定产物：

```bash
source .venv/bin/activate
python scripts/import_ctf_captures.py \
  --capture-meta outputs/ctf_resource_crawls/<批次>/<目标>/capture_meta.json
```

调度器执行已配置的 `ctf_resource_crawl` 时会自动导入，除非 runner 传入 `--no-import`。

## 常用校验 SQL

查看 MediaCrawler 内容入库量：

```sql
SELECT platform_key, COUNT(*) AS posts
FROM web_posts
GROUP BY platform_key
ORDER BY platform_key;
```

查看作者字段和互动数：

```sql
SELECT
  platform_key,
  author_display_name,
  platform_post_id,
  published_at,
  post_likes_count,
  post_favorites_count,
  post_comments_count,
  post_shares_count,
  post_views_count,
  keyword
FROM web_posts
ORDER BY captured_at DESC
LIMIT 20;
```

查看正文图片来源与本地证据：

```sql
SELECT
  p.platform_key,
  p.platform_post_id,
  i.image_role,
  i.image_index,
  i.image_url,
  i.local_path,
  i.width,
  i.height,
  i.mime_type,
  i.sha256
FROM web_posts p
JOIN web_post_images i ON i.web_post_id = p.id
ORDER BY p.captured_at DESC, i.image_index
LIMIT 50;
```

查看页面级证据底座：

```sql
SELECT
  site_key,
  ok,
  published_at,
  saved_images,
  total_image_requests,
  capture_meta_path
FROM ctf_captures
ORDER BY captured_at DESC
LIMIT 20;
```

查看调度器最近运行：

```sql
SELECT
  run_id,
  status,
  jobs_selected,
  completed_count,
  failed_count,
  blocked_count,
  report_path
FROM crawl_run_reports
ORDER BY id DESC
LIMIT 10;
```

## 校验逻辑

`import_ctf_captures.py` 会对页面级产物做基础校验，并把成功且内容就绪的页面归一化到 `web_posts`：

- `site_key`、`target_url`、`artifact_dir` 必须存在。
- `capture_meta.json`、`rendered.html`、`visible_text.txt`、`images.json` 等引用文件应存在。
- 新产物的 `images.json` 与 `failed_images.json` 必须为空，`image_summary.saved_images=0`；历史未分类
  图片记录只产生忽略警告，不检查文件、不导入关系。
- flag-like 文本只做格式校验，不在抓取阶段清洗。
- `skipped=true` 的视频跳过产物只保留为运行证据，不导入 `ctf_captures`，也不生成 `web_posts`。

MediaCrawler 入库采用去重更新：

- 优先用 `(platform_key, platform_post_id)` 匹配旧记录。
- 没有平台 ID 时用 `(platform_key, canonical_url)` 匹配旧记录。
- 插入或更新前先把全部 `MaterializedImage` 元数据和本地文件重新验证；一张正文图缺少有效本地
  文件即拒绝整帖。
- 更新帖子时在同一 SQLite savepoint 删除并重建该帖子的 `web_post_images` 行；匹配到相同稳定
  资产或源顺序的既有有效文件时可以保留其本地证据，不能因为新 JSONL 只有 URL 而清空路径。
- schema bootstrap 提交完成后、第一帖 SAVEPOINT 前显式开启批次外层事务；逐帖 `RELEASE
  SAVEPOINT` 不得成为批次提交点。第二帖或更后帖子失败时，外层事务必须把此前帖子和图片关系一并
  回滚，禁止摘要报告 0 行而数据库存在部分批次。
- 主表更新、图片关系重建任一步异常都会回滚 savepoint，不留下“帖子已更新、图片未完成”的
  半提交状态。文件晋升采用内容寻址和原子替换，并且只在所有完成门禁通过后开始；单文件、后续
  图片或可确认发生在数据库提交前的事务失败删除本轮新建文件，既有同 SHA 复用文件不删除。提交前
  导入失败在摘要写 `sqlite_import_failed` 与回滚计数，并且不推进发现 checkpoint。数据库成功提交
  或提交结果不能安全判定时保留文件，先核对 SQLite 引用再恢复控制面，不能以清理孤儿为由删除
  可能已被正式图片关系引用的文件。
- 正式媒体的首次晋升、SQLite 提交和失败回滚属于同一个跨进程互斥区间；不同平台、不同 XHS 账号
  或不同 runner 不得并发交错这三个动作。回滚删除 `reused=false` 文件前仍须检查当前数据库的
  `web_post_images.local_path`，已有提交引用时保留文件。晋升期间的 `KeyboardInterrupt`、`SystemExit`
  等可捕获进程中断必须先回滚此前已晋升的新文件，再继续传播中断。进入 SQLite 批次事务后、成功
  提交前的同类中断按提交前导入失败处理：整批数据库与新媒体回滚，返回 `sqlite_import_failed`
  摘要而不再传播；提交成功后的中断继续传播并保留引用文件。不可捕获的 `SIGKILL` 不作完成承诺。
- 除已知头像键及同记录内经这些键证明的重复头像 URL 外，清除后的 JSONL 结构完整保存在
  `raw_sample_json`，便于后续清洗补字段。

## 历史数据说明

B站 2026-08-02 正文完整性事件已经完成回填和清理，当前入库路径不再依赖历史修复入口。范围、
最终数量、证据和处置只在[B站正文完整性事件](incidents/2026-08-02-bilibili-article-completeness.md)
维护。现行入库验证命令统一在[运行手册](operations-runbook.md)维护，本文件不保留一次性修复或
临时数据库命令。

## 目前边界

- 当前规则禁止视频功能。项目侧 `--get-media` 会直接失败；五平台正式 runner 全部强制
  `--download-images`，同时保持 MediaCrawler 视频保存关闭，抖音图片路径不会回退到视频或音乐下载。
- 正式新记录只下载并以 `content` 角色导入显式投影的正文图。头像、作者主页、封面、搜索预览、
  视频、音乐和知乎公式图会在下载前自动忽略，不进入 manifest、`data/media`、项目 JSONL、摘要
  子进程日志或 SQLite；历史未清除产物在恢复读取时也必须先在内存中清除头像数据。非结构化
  stdout/stderr 出现任一已知头像键时整段丢弃为审计标记，不按 URL 域名或路径猜测。
- 知乎当前是 MediaCrawler 入库路径；页面级产物只作为临时排障证据，不作为默认调度链路。
- 正式有效性过滤在入库前完成；业务清洗、低质量分级和 flag-like 误报处理仍放在 SQL 视图或下游清洗层。
- `outputs/` 不是长期图片主存储。结构化内容、作者、互动数、URL、本地路径、状态和摘要应进入
  SQLite；正式正文图片长期保存在 `data/media`，`outputs/` 只保留 manifest、必要日志、报告和
  可清理 staging。
- `temp/` 不参与正式入库和调度，只保存临时验证数据库或一次性测试结果。代码不能依赖 `temp/` 中已有文件。
