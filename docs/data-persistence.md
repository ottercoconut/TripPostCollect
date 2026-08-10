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

相关 schema：

| 文件 | 表 |
|---|---|
| `db/source_platforms.sql` | `source_platforms` |
| `db/web_posts.sql` | `web_posts`、`web_post_images` |
| `db/ctf_captures.sql` | `ctf_captures`、`ctf_capture_images` |
| `db/crawl_scheduler.sql` | `crawl_jobs`、`crawl_discovery_checkpoints`、`crawl_discovery_seen_candidates`、`crawl_attempts`、`crawl_run_reports`、`profile_health_checks`；任务类型包含通用搜索和页面证据 |
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
当前配置中的历史调度任务。现行项目范围由操作人或 Agent 在计划冻结时核对；配置解析器、抓取器
和导入器不按关键词文本设置硬门禁。“崂山攻略”等不含“青岛”字样但明确属于青岛范围的关键词
可以正常入库。

`crawl_discovery_checkpoints` 和 `crawl_discovery_seen_candidates` 是 B站、微博、抖音和知乎正式
搜索的控制面记忆，不是内容表。
`job_id + query_fingerprint` 唯一定位同一来源查询；`resume_page` 保存下一安全页，抖音同时使用
`resume_offset` 和 `resume_cursor`，`last_stop_reason` 与 `last_stop_detail` 保存停止分类，
`last_summary_path` 指向尚未达到目标的累计摘要，
`campaign_candidate_count` 保存累计报告数。`status=exhausted` 表示已保存深层前沿明确耗尽，
后续默认只做顶部刷新；抖音若刷新同时证明存在持久记忆中没有的新候选 ID、`has_more=true` 与可继续的新 search ID，则从刷新链
建立新前沿并把 checkpoint 恢复为 `active`。checkpoint 只能在 child 摘要形成后提交；诊断
`--no-import` 不得更新它。内容仍只在
完整目标达到后写入 `web_posts` / `web_post_images`。通用已完成处理候选表按 job 与查询指纹保存
视频、有决定性证据的字段无效候选和有效候选 ID；它只用于发现去重，不把无效候选变成内容记录。
可恢复请求失败不属于已完成处理，尤其不能把 B站详情失败 ID 写入该表。

`web_posts` 是统一内容主表，面向用户查询和后续数据使用。`ctf_captures` 是证据和调试底座，面向程序脚本或 Agent 排查抓取过程。页面级抓取成功后，也会归一化生成 `web_posts` 行，并通过 `web_posts.source_capture_id` 关联对应 `ctf_captures.id`。

当前结构不再保留“只入 `ctf_captures`、不入 `web_posts`”的内容形态。已有成功且内容就绪的页面级证据记录，应通过 `import_ctf_captures.py` 重新导入或同步，使用户查询统一落在 `web_posts` 上。

`xhs_*` 表只保存小红书控制面和审计信息，不替代内容主表：`xhs_accounts` 保存账号状态、
隔离 profile 路径、加密状态路径和身份哈希；`xhs_account_leases` 只防止同一账号并发使用；
`xhs_account_events`、`xhs_runs` 保存人工状态变化、挑战信号和运行摘要。Cookie、localStorage
原文和加密密钥不写 SQLite。`xhs_discovery_checkpoints` 按目标、账号和查询指纹保存下一安全页、
该深层搜索的 `search_id`、耗尽状态、停止原因和未完成累计摘要路径；它不保存 Cookie，也不跨
账号共享未入库活动。`xhs_discovery_seen_candidates` 在相同作用域保存已经完成处理的笔记 ID，
包括因视频或正式字段不足而不进入累计摘要的候选，避免它们跨轮反复触发详情和作者请求。
小红书有效图文仍写入 `web_posts` / `web_post_images`。

小红书 checkpoint 与通用表遵守同一提交边界：只有 child `summary.json` 已形成且含分页证据时，
`xhs_runner.py` 才在同一事务提交前沿与已处理候选 ID；未达到目标时 `last_summary_path` 指向
合并活动的最新摘要，达到完整目标并成功入库后清空摘要路径但保留深层前沿。
`status=exhausted` 后只刷新顶部；`--no-import` 不更新
checkpoint。`import_result.reason=sqlite_import_failed` 时 runner 和 discovery 提交函数都必须跳过
checkpoint、seen 与 campaign 更新。摘要或其 JSONL 缺失时冻结失败，不能静默丢弃活动。

## 模型执行抓取持久化规范

其它模型或 Agent 接手抓取、导入、补字段任务时，必须把本节当作执行清单，而不是只读架构说明。

### 任务前必读

1. 先读 `docs/README.md`，确认当前平台分工和常用命令。
2. 再读本文件，确认数据应进入 `web_posts`、`web_post_images`、`ctf_captures` 还是 `ctf_capture_images`。
3. 字段能力不确定时读 `docs/platform-field-coverage.md`，不要凭平台印象推断字段是否应该存在。
4. 通用调度不确定时读 `docs/crawl-architecture.md`、`config/crawl_targets.json` 和
   `scripts/crawl_runner.py`；小红书读 `docs/platforms/xhs.md`、`config/xhs_*.json` 和
   `scripts/xhs_runner.py`。

### 入口选择

| 目标 | 正确入口 | 入库责任 |
|---|---|---|
| 通用平台正式抓取或落库 | `scripts/crawl_runner.py` | 冻结状态后调用对应执行器并验证持久化 |
| 小红书正式抓取或落库 | `scripts/xhs_runner.py` | 租赁人工指定账号、冻结状态、调用底层执行器并验证持久化 |
| 通用 MediaCrawler 字段诊断 | `scripts/mediacrawler_crawl.py --no-import` | 不作为正式入库或完成证据；不接受小红书独立账号链路 |
| 页面执行器开发验证 | `scripts/ctf_resource_crawl.py` | 只验证产物；当前配置没有页面证据正式任务 |
| 只刷新调度库 | `scripts/crawl_runner.py --sync-only` | 只同步平台和任务，不抓取内容 |

不要直接调用 `tools/MediaCrawler` 作为根项目命令；根项目必须通过 `scripts/mediacrawler_crawl.py` 封装入口统一处理登录态、视频跳过、输出目录、摘要和 SQLite 导入。

### 执行规则

- 运行 Python 前先执行 `source .venv/bin/activate`，激活后只用 `python`。
- 默认数据库是 `data/trippostcollect.sqlite`；验证高风险变更时先用 `--db temp/<name>.sqlite`。
- 正式流程不得依赖 `temp/` 里已有文件；`temp/` 只保存一次性验证产物，用完提取结论后清理。
- 路径必须从脚本现有参数或 `trippostcollect.core.paths` 解析，不硬编码输出目录、浏览器 profile 或运行状态目录。
- 视频目标、视频媒体请求和明确视频记录必须跳过，不得写入 `web_posts`。
- 五个平台的正式结构化 child 必须由 runner 注入 `--download-images` 和正式 `--media-root`；不得
  使用被禁用的 `--get-media`，也不得关闭图片要求完成 URL-only 入库。
- 图片候选只来自平台权威正文图字段。头像、作者主页、封面、搜索预览、视频、音乐和知乎公式图
  在下载前排除；不得靠下载后尺寸、文件名或人工清理作为正式过滤手段。
- 知乎仅对 `zhimg.com` 已知 URL 变换后缀按稳定资源路径去重，例如同 token 的 `_r`、`_720w`、
  `_1440w`；同帖保留首次 URL，原始变体仍在内容原始证据中。该平台规则不使用视觉相似度，也不
  作用于外部域名。
- 正式运行前用 `df -h data` 检查数据库、staging 和长期图片目录所在卷的可用空间。容量不足时在
  child 前停止；不得在图片部分晋升后继续勉强入库。
- `published_at` 必须来自平台原始发帖时间；缺明确证据时保持 NULL，不能用 `captured_at` 或导入时间补。
- 页面级错误页、搜索页、中间页和验证码页只保留证据，不生成用户内容记录。
- `web_posts` 面向用户查询；`ctf_captures` 面向证据和调试。不要让用户内容只停留在 `ctf_captures`。
- 正式结构化任务必须配置 `candidate_hard_limit`、`target_new_posts`、`top_refresh_max_pages` 和字段
  profile；正常默认模式中，候选上限是每次 child 的未知候选安全预算，实际候选从 0 按需增长，
  达到新增目标后立即停止，不要求抓满上限。只有跨次累计的 `valid_new_count >= target_new_posts`
  且实际新增行数达标才算达到数量目标。用户显式要求的单轮 `source-exhausted` 模式不改这些配置，
  只把可验证来源耗尽作为该轮替代完成门禁。
- 固定 URL 页面证据任务只代表一个页面。以后若新增正式任务，必须在
  `config/crawl_targets.json` 声明 `job_kind=ctf_resource_crawl` 并从 `crawl_runner.py` 进入。

### 标准执行流程

先查看调度计划，不抓取：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --completion-mode target-new-posts \
  --max-jobs 5
```

这里的“不抓取”只表示不访问平台内容、不写 `web_posts`；默认仍会把配置同步到调度表，并写
`crawl_run_reports`、run summary 和 execution state。若只想用已经同步的调度表冻结计划，显式
使用运行手册中的 `--no-sync-config`。

执行到期任务：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --completion-mode target-new-posts \
  --max-jobs 3
```

小红书先完成隔离账号登录，再由操作人选择账号运行；完整命令见 `docs/platforms/xhs.md`：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --dry-run \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --completion-mode target-new-posts
```

单平台字段诊断命令见 `docs/operations-runbook.md`。诊断参数不得复制到正式配置，
也不能绕过冻结状态直接入库。

导入指定页面级产物：

```bash
source .venv/bin/activate
python scripts/import_ctf_captures.py \
  --capture-meta outputs/ctf_resource_crawls/<批次>/<目标>/capture_meta.json
```

### 成功标准

一次抓取或导入不能只看命令退出码。先按入口判断，不能混用完成语义：

- 通用正式结构化任务必须有 `summary.json`，runner 任务状态为 `completed`，冻结状态五阶段
  全部完成；默认数量模式要求 `formal_validation.new_target_met=true`、
  `import_new_target_met=true`，显式来源耗尽模式改为要求
  `formal_validation.source_exhausted_met=true` 并完成真实入库。
- 小红书正式任务必须有顶层 `run_summary.json` 和 child `summary.json`，顶层任务状态为
  `completed`，冻结状态五阶段全部完成；完成门禁同样按本轮默认数量模式或显式来源耗尽模式判断。
- 任意入口的 `--no-import` 和直接运行 `ctf_resource_crawl.py` 都是诊断或开发验证。即使退出码
  为 0、摘要或冻结状态显示 `completed`、产物完整，也不能汇报为正式轮次完成；正式完成必须
  直接核对 `import_result.inserted_rows`，不能只看 `import_new_target_met`。
- 以后若配置固定 URL 正式任务，其 runner 状态和冻结阶段必须为 `completed`，页面证据必须成功
  导入；成功只代表该 URL，不代表平台批量完成。
- 正式入库的 `processed_rows` 等于本轮有效集合大小。
- `processed_rows`、`inserted_rows`、`updated_rows` 分别报告；只有 `inserted_rows` 可以兑现 `valid_new_count`，更新行不能计入新增目标。
- `updated_rows` 是 upsert 命中已有 `web_posts` 的数量：优先按
  `(platform_key, platform_post_id)`，平台 ID 缺失时按 `(platform_key, canonical_url)`；
  本轮覆盖主表字段并删除后重建该帖的 `web_post_images`，不增加主表总行数。
- 新增/更新记录出现在 `web_posts`；每张正文图在 `web_post_images` 同时具有来源 URL、角色、连续
  顺序、非空 `local_path`、尺寸、真实 MIME 和 SHA-256，且路径对应 `data/media` 内有效文件；同帖
  正文图 SHA-256 必须唯一。
- 页面级证据出现在 `ctf_captures`，成功且内容就绪的详情页同步生成 `web_posts`。
- 关键字段符合平台能力表：文本/标题、平台原始 ID/URL、发布时间、作者字段、图片 URL、互动指标按平台应有尽有。
- 明确视频记录只计入跳过，不作为失败记录写入内容主表。
- `summary.json.image_materialization.complete=true`，候选帖、完整帖和图片下载/验证/晋升计数满足
  正式契约等式；runner 的 `artifacts_verified` 与 `persistence_verified` 均完成。

### 最低 SQL 校验

每次真实入库后至少执行以下检查：

```bash
source .venv/bin/activate
python - <<'PY'
import sqlite3

conn = sqlite3.connect("data/trippostcollect.sqlite")
conn.row_factory = sqlite3.Row

queries = {
    "platform_counts": """
        SELECT platform_key, COUNT(*) AS posts
        FROM web_posts
        GROUP BY platform_key
        ORDER BY platform_key
    """,
    "recent_records": """
        SELECT platform_key, platform_post_id, title, published_at,
               author_display_name, author_followers_count,
               post_images_count, captured_at
        FROM web_posts
        ORDER BY captured_at DESC
        LIMIT 20
    """,
    "zhihu_followers": """
        SELECT COUNT(*) AS total,
               SUM(author_followers_count IS NOT NULL) AS with_followers
        FROM web_posts
        WHERE platform_key='zhihu'
    """,
}

for name, sql in queries.items():
    print(f"## {name}")
    for row in conn.execute(sql):
        print(dict(row))

# 使用 run_summary.json 中 records[].artifact_dir 的绝对路径，只核验本轮导入；不要省略该条件，
# 否则历史旧格式记录会污染本轮结论。
run_artifact_dir = "/absolute/path/from/run_summary/records/artifact_dir"
run_queries = {
    "structured_follower_evidence": """
        SELECT
          platform_key,
          COUNT(*) AS total,
          SUM(author_followers_count IS NULL) AS followers_null,
          SUM(json_extract(author_json, '$.followers_observed') IS NOT 1) AS followers_unobserved,
          SUM(
            json_extract(author_json, '$.followers_source') IS NOT
            CASE platform_key
              WHEN 'bilibili' THEN 'relation_stat'
              WHEN 'weibo' THEN 'search_author'
              WHEN 'zhihu' THEN 'search_author'
              WHEN 'xhs' THEN 'creator_profile'
              WHEN 'douyin' THEN 'creator_profile'
            END
          ) AS invalid_follower_source
        FROM web_posts
        WHERE artifact_dir=?
          AND platform_key IN ('bilibili', 'weibo', 'zhihu', 'xhs', 'douyin')
        GROUP BY platform_key
        ORDER BY platform_key
    """,
    "content_image_relations": """
        WITH content_images AS (
          SELECT web_post_id, COUNT(*) AS image_rows
          FROM web_post_images
          WHERE image_role='content'
          GROUP BY web_post_id
        )
        SELECT
          p.platform_key,
          COUNT(*) AS total,
          SUM(COALESCE(i.image_rows, 0) = 0) AS without_content_images,
          SUM(COALESCE(i.image_rows, 0) != p.post_images_count) AS image_count_mismatches
        FROM web_posts p
        LEFT JOIN content_images i ON i.web_post_id=p.id
        WHERE p.artifact_dir=?
        GROUP BY p.platform_key
        ORDER BY p.platform_key
    """,
    "local_image_evidence": """
        SELECT
          p.platform_key,
          COUNT(*) AS image_rows,
          SUM(i.local_path IS NULL OR i.local_path='') AS missing_local_path,
          SUM(i.width IS NULL OR i.height IS NULL) AS missing_dimensions,
          SUM(i.mime_type IS NULL OR i.sha256 IS NULL) AS missing_integrity
        FROM web_post_images i
        JOIN web_posts p ON p.id=i.web_post_id
        WHERE p.artifact_dir=? AND i.image_role='content'
        GROUP BY p.platform_key
        ORDER BY p.platform_key
    """,
}

for name, sql in run_queries.items():
    print(f"## {name}")
    for row in conn.execute(sql, (run_artifact_dir,)):
        print(dict(row))
PY
```

### 失败处理

- B站、微博、抖音或知乎登录态缺失时运行 `login_warmup.py`；小红书只运行所选账号的
  `xhs_login.py`。页面证据使用独立 profile，当前不由统一 warmup 验证。
  不要临时改抓取脚本绕过登录判断或复用其他账号 profile。
- 字段缺失时，先检查 JSONL 顶层字段、`raw_sample_json` 和平台字段覆盖表；确认来源字段存在但没入库，再改导入映射。
- 来源字段根本不存在时，先用浏览器或 API 定位字段来源，再补抓取器；不要在入库层造数。
- 图片失败先读 `formal_validation.pagination_evidence.stop_event.deferred_retryable_failures`、
  `candidate_deferred` 和 manifest 对应行，再检查 staging 文件、平台日志尾部及登录态。有限重试仍
  失败的整帖暂时跳过，失败 ID 不写候选记忆；child 继续后续候选，checkpoint 回到最早失败坐标。
  若既定完成条件仍未满足，本轮保留累计摘要而不做部分入库。身份、路径、格式、哈希或尺寸错误必须
  修复产物链路，禁止删 manifest 行、改摘要或只写 URL。
- 页面级抓取遇到错误页时，保留 `ctf_captures` 和 artifact，导入层过滤 `web_posts`。
- 默认库需要清理脏数据时，先复制 `data/trippostcollect.sqlite` 到 `data/backups/`，再执行受控 SQL。
- 若一次路径连续 2-3 次无法拿到目标字段，应换到平台 API、作者主页、已有 artifact 或调度链路，不要反复扩大同一个失败抓取。

## MediaCrawler 结果入库

微博、抖音、知乎等通用结构化结果由 `scripts/mediacrawler_crawl.py` 调用 MediaCrawler 后
导入 `web_posts`；小红书由 `xhs_runner.py` 为人工指定账号申请互斥租约并解密会话后调用同一底层执行器。
五个平台都在当前登录/签名会话中把权威正文图下载到本轮 staging，原子生成 schema v1
`image_manifest.jsonl`；根项目按同一显式投影复验 manifest、文件字节和身份。只有数量或来源耗尽、
字段、行为、策略和 staging 图片门禁全部通过，正式运行才晋升到 `data/media` 并注入统一入库映射；
门禁未通过、晋升失败或已确认发生在提交前的 SQLite 导入回滚时，不得留下本轮新建的无引用长期媒体文件。平台显式投影后的全部 manifest 候选均须通过下载与字节复验；
知乎已知 `zhimg` 尺寸 URL 变体在投影时按资源路径合并，不重复生成 manifest。下载后再仅在同帖内
按 SHA-256 保留首次来源并记录重复来源证据。任何图片失败都使该整帖失去正式资格。只有有限重试
耗尽后的 `image_download_retryable` 可在写完 manifest 与 `candidate_deferred` 后继续其他候选；
格式、解码、大小或明确非重试 HTTP 等终态错误写完失败 manifest 后停止 child 并保留当前前沿。
若后续候选达到默认新增目标，只导入
图片完整的正式有效集合；未达到目标或处于显式来源耗尽模式时，存在 deferred 候选会使本轮保持
`deferred_retry_pending`，不得入库或宣称来源耗尽。
分页证据或任一 child 表明 `runtime_failed`、登录或验证码阻断时，即使有效新增数已经达到目标，也必须
保持 `completion_met=false`，不得晋升或入库；运行失败优先于数量完成。
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
| `web_post_images`（`author_avatar`） | 可选作者头像 URL 参考；`local_path` 等本地字段为空，不下载、不进 manifest、不计入 `post_images_count` 或正文图完整性 |
| `raw_sample_json` | MediaCrawler 原始 JSONL 行；正式记录必须含 `content_detail_status` 和 `content_detail_source` |

代码在下载前用五个平台显式投影识别正文图，在导入边界识别其他同类字段差异；内部持久化结构
统一写入 `web_posts` / `web_post_images`。视频记录只用于识别和跳过，不进入内容主表。

显式使用 `--recovery-keyword` 人工续跑时，最终摘要会合并旧、新两轮记录，
正常记录的 `web_posts.keyword` 会逐条保存真实来源，因此同一个最终 `artifact_dir` 可以同时
出现原关键词和恢复关键词。若旧记录缺少 `source_keyword`，必须结合原摘要和 JSONL 审计，
不能把回退到最终摘要的值解释成原始检索词。

系统不再保存 `city_name`，也不做城市别名解析、关键词硬门禁或正文地名推断。项目范围以该轮
冻结的任务配置和操作审计为准。
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
    "new_target_met": true,
    "stop_reason": "target_new_met",
    "image_materialization_complete": true
  },
  "image_materialization": {
    "required": true,
    "promotion_required": true,
    "candidate_posts": 65,
    "complete_posts": 65,
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
    "processed_rows": 65,
    "inserted_rows": 50,
    "updated_rows": 15
  }
}
```

## 页面级抓取结果入库

B站 Opus 详情页由 `ctf_resource_crawl.py` 保存页面证据，再由 `import_ctf_captures.py` 同步写入两层数据：`ctf_captures` / `ctf_capture_images` 作为证据和调试底座，`web_posts` / `web_post_images` 作为用户使用的统一内容主表。页面级抓取会从 `article:published_time`、JSON-LD、`time[datetime]` 等明确页面元数据中提取 `published_at`，并在抓取元数据中直接保存为 Asia/Shanghai ISO 字符串；B站 Opus/图文页还会从可见文本中的明确日期行提取。没有明确证据时保持为空，不用抓取时间替代。截图属于证据附件，截图失败会记录到 `artifact_errors`，但只要页面内容、文本和图片资源已成功采集，不应把整条内容标成抓取失败。

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
  --max-image-save 3 \
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
- `images.json` 中的图片统计要和 `image_summary` 对得上。
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
  `web_post_images.local_path`，已有提交引用时保留文件。
- 原始 JSONL 行完整保留在 `raw_sample_json`，便于后续清洗补字段。

## B站历史摘要回填

本节记录 2026-08-02 正文完整性事件的历史回填约束。事件处理时，既有 B站摘要记录已经存在于
`web_posts` 和候选记忆，正常正式搜索会在详情前跳过这些 ID，因此不能靠重跑关键词自动修复。
历史回填使用独立、幂等的详情修复流程：

1. 写默认库前创建 SQLite 一致性备份；先在临时库用固定小样本验证。
2. 从 `web_posts` 读取现有 B站平台 ID 与原关键词，分批请求详情并保存可恢复进度；不删除或修改
   `crawl_discovery_checkpoints`、`crawl_discovery_seen_candidates` 和 `crawl_jobs`。
3. 只有详情正文与正文图片门禁通过时才按相同平台 ID upsert。主表行数不得增加，原关键词必须
   保留，`raw_sample_json` 必须能区分原搜索摘要与本次详情来源。
4. 限流、超时、请求或解析失败保持待重试，不覆盖现有行并假称完整；明确删除、私密或永久不可用
   单独报告，不伪造正文。
5. 回填报告分别列出计划、详情成功、更新、永久无效、待重试和解析错误数量，并验证正文长度、
   图片关系、唯一 ID、外键及 SQLite 完整性。

回填属于历史数据修复，不兑现新抓取的 `target_new_posts`，也不改变任何来源耗尽结论。具体事故
范围与阶段记录见
[`incidents/2026-08-02-bilibili-article-completeness.md`](incidents/2026-08-02-bilibili-article-completeness.md)，
执行顺序、检查点和验收门槛见
[`plans/2026-08-02-bilibili-full-library-repair.md`](plans/2026-08-02-bilibili-full-library-repair.md)。
历史冻结范围完成后，用户于 2026-08-07 明确要求从当前业务数据删除 3 条未成功修复记录。当前
默认库保留的 3,006 条 B站记录全部具有 `content_detail_status=detail_observed`；原始 artifact、
sidecar、修复报告和备份仍作为历史审计/恢复证据，不属于当前内容数据。

## 手工验证步骤

用临时库验证，不污染默认库：

```bash
source .venv/bin/activate
python scripts/mediacrawler_crawl.py \
  --platforms weibo \
  --keyword 青岛旅游 \
  --candidate-hard-limit 20 \
  --target-new-posts 1 \
  --timeout-per-platform 180 \
  --download-images \
  --media-root temp/mediacrawler_import_verify_media \
  --db temp/mediacrawler_import_verify.sqlite
```

查询结果：

```bash
sqlite3 temp/mediacrawler_import_verify.sqlite \
  "select platform_key,count(*) from web_posts group by platform_key;"
```

显式验证调度同步：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --db temp/scheduler_verify.sqlite \
  --sync-only
```

查看计划命令：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --db temp/scheduler_verify.sqlite \
  --dry-run \
  --completion-mode target-new-posts \
  --max-jobs 5
```

## 目前边界

- 当前规则禁止视频功能。项目侧 `--get-media` 会直接失败；五平台正式 runner 全部强制
  `--download-images`，同时保持 MediaCrawler 视频保存关闭，抖音图片路径不会回退到视频或音乐下载。
- 正式新记录只下载并以 `content` 角色导入显式投影的正文图。头像、作者主页、封面、搜索预览、
  视频、音乐和知乎公式图会在下载前自动忽略，不进入 manifest 或 `data/media`；作者头像可以保留
  为 `author_avatar` URL 参考关系，但本地字段为空且不参与正文图完整性。
- 知乎当前是 MediaCrawler 入库路径；页面级产物只作为临时排障证据，不作为默认调度链路。
- 正式有效性过滤在入库前完成；业务清洗、低质量分级和 flag-like 误报处理仍放在 SQL 视图或下游清洗层。
- `outputs/` 不是长期图片主存储。结构化内容、作者、互动数、URL、本地路径、状态和摘要应进入
  SQLite；正式正文图片长期保存在 `data/media`，`outputs/` 只保留 manifest、必要日志、报告和
  可清理 staging。
- `temp/` 不参与正式入库和调度，只保存临时验证数据库或一次性测试结果。代码不能依赖 `temp/` 中已有文件。
