# 入库与校验

## 数据库文件

默认数据库：

```text
data/trippostcollect.sqlite
```

相关 schema：

| 文件 | 表 |
|---|---|
| `db/source_platforms.sql` | `source_platforms` |
| `db/web_posts.sql` | `web_posts`、`web_post_images` |
| `db/ctf_captures.sql` | `ctf_captures`、`ctf_capture_images` |
| `db/crawl_scheduler.sql` | `crawl_jobs`、`crawl_attempts`、`crawl_run_reports`、`profile_health_checks` |

`trippostcollect.db.bootstrap` 是统一实现。`crawl_runner.py`、MediaCrawler 入库和 CTF artifact 导入都会自动执行 bootstrap，补齐 schema、`source_platforms` 和 `crawl_jobs` 配置；手工 `--sync-only` 只用于显式刷新或排查。

`web_posts` 是统一内容主表，面向用户查询和后续数据使用。`ctf_captures` 是证据和调试底座，面向程序脚本或 Agent 排查抓取过程。页面级抓取成功后，也会归一化生成 `web_posts` 行，并通过 `web_posts.source_capture_id` 关联对应 `ctf_captures.id`。

当前结构不再保留“只入 `ctf_captures`、不入 `web_posts`”的内容形态。已有成功且内容就绪的页面级证据记录，应通过 `import_ctf_captures.py` 重新导入或同步，使用户查询统一落在 `web_posts` 上。

## 模型执行抓取持久化规范

其它模型或 Agent 接手抓取、导入、补字段任务时，必须把本节当作执行清单，而不是只读架构说明。

### 任务前必读

1. 先读 `docs/README.md`，确认当前平台分工和常用命令。
2. 再读本文件，确认数据应进入 `web_posts`、`web_post_images`、`ctf_captures` 还是 `ctf_capture_images`。
3. 字段能力不确定时读 `docs/platform-field-coverage.md`，不要凭平台印象推断字段是否应该存在。
4. 调度或任务选择不确定时读 `docs/crawl-architecture.md`、`config/crawl_targets.json` 和 `scripts/crawl_runner.py`。

### 入口选择

| 目标 | 正确入口 | 入库责任 |
|---|---|---|
| 任一正式抓取或落库任务 | `scripts/crawl_runner.py` | 冻结状态后调用对应执行器并验证持久化 |
| MediaCrawler 字段诊断 | `scripts/mediacrawler_crawl.py --no-import` | 不作为正式入库或完成证据 |
| 页面执行器开发验证 | `scripts/ctf_resource_crawl.py` | 只验证产物；正式任务仍由调度器进入 |
| 只刷新调度库 | `scripts/crawl_runner.py --sync-only` | 只同步平台和任务，不抓取内容 |

不要直接调用 `tools/MediaCrawler` 作为根项目命令；根项目必须通过 `scripts/mediacrawler_crawl.py` 封装入口统一处理登录态、视频跳过、输出目录、摘要和 SQLite 导入。

### 执行规则

- 运行 Python 前先执行 `source .venv/bin/activate`，激活后只用 `python`。
- 默认数据库是 `data/trippostcollect.sqlite`；验证高风险变更时先用 `--db temp/<name>.sqlite`。
- 正式流程不得依赖 `temp/` 里已有文件；`temp/` 只保存一次性验证产物，用完提取结论后清理。
- 路径必须从脚本现有参数或 `trippostcollect.core.paths` 解析，不硬编码输出目录、浏览器 profile 或运行状态目录。
- 视频目标、视频媒体请求和明确视频记录必须跳过，不得写入 `web_posts`。
- `published_at` 必须来自平台原始发帖时间；缺明确证据时保持 NULL，不能用 `captured_at` 或导入时间补。
- 页面级错误页、搜索页、中间页和验证码页只保留证据，不生成用户内容记录。
- `web_posts` 面向用户查询；`ctf_captures` 面向证据和调试。不要让用户内容只停留在 `ctf_captures`。
- 正式结构化任务必须配置 `candidate_hard_limit`、`target_new_posts` 和字段 profile；只有 `valid_new_count >= target_new_posts` 且实际新增行数达标才算达到本轮目标。
- 固定 URL 页面证据任务只代表一个页面，不能汇报为平台级批量抓取完成。

### 标准执行流程

先查看调度计划，不抓取：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --max-jobs 5
```

执行到期任务：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --max-jobs 3
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

一次抓取或导入不能只看命令退出码。必须同时满足：

- 有 `summary.json` 或 `run_summary.json`，且平台任务状态不是 failed。
- 正式入库的 `processed_rows` 等于本轮有效集合大小；诊断 `--no-import` 必须明确标为非正式。
- 正式结构化任务的冻结状态为 `completed`，且 `formal_validation.new_target_met` 和 `import_new_target_met` 都为 true。
- `processed_rows`、`inserted_rows`、`updated_rows` 分别报告；只有 `inserted_rows` 可以兑现 `valid_new_count`，更新行不能计入新增目标。
- `updated_rows` 是 upsert 命中已有 `web_posts` 的数量：优先按
  `(platform_key, platform_post_id)`，平台 ID 缺失时按 `(platform_key, canonical_url)`；
  本轮覆盖主表字段并删除后重建该帖的 `web_post_images`，不增加主表总行数。
- 新增/更新记录出现在 `web_posts`，图片 URL 出现在 `web_post_images`。
- 页面级证据出现在 `ctf_captures`，成功且内容就绪的详情页同步生成 `web_posts`。
- 关键字段符合平台能力表：文本/标题、平台原始 ID/URL、发布时间、作者字段、图片 URL、互动指标按平台应有尽有。
- 明确视频记录只计入跳过，不作为失败记录写入内容主表。
- 已知错误页没有进入 `web_posts`，例如去哪儿“页面不存在，可能已被删除”。

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
    "qunar_error_pages": """
        SELECT COUNT(*) AS rows
        FROM web_posts
        WHERE platform_key='qunar'
          AND content_text LIKE '%非常抱歉，您访问的页面不存在，可能已被删除%'
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
PY
```

### 失败处理

- 登录态缺失或失效时，先运行对应 `mediacrawler_login_warmup.py`，不要临时改抓取脚本绕过登录判断。
- 字段缺失时，先检查 JSONL 顶层字段、`raw_sample_json` 和平台字段覆盖表；确认来源字段存在但没入库，再改导入映射。
- 来源字段根本不存在时，先用浏览器或 API 定位字段来源，再补抓取器；不要在入库层造数。
- 页面级抓取遇到错误页时，保留 `ctf_captures` 和 artifact，导入层过滤 `web_posts`。
- 默认库需要清理脏数据时，先复制 `data/trippostcollect.sqlite` 到 `data/backups/`，再执行受控 SQL。
- 若一次路径连续 2-3 次无法拿到目标字段，应换到平台 API、作者主页、已有 artifact 或调度链路，不要反复扩大同一个失败抓取。

## MediaCrawler 结果入库

微博、小红书、抖音、知乎等非视频结构化结果由 `scripts/mediacrawler_crawl.py` 调用 MediaCrawler 后导入 `web_posts`。微博 store 会保留搜索结果中的 `mblog.pics` 图片 URL 和 `mblog.user.followers_count/followers_count_str`，用于图文和作者粉丝量校验。小红书搜索会补拉作者主页指标，用于填充粉丝数等作者字段。知乎搜索结果中的回答/文章 `created_time` 会映射到 `published_at`，正文 HTML 图片会在清洗为纯文本前保存为 `image_list/image_count` 并写入 `web_post_images`，搜索结果 `author.follower_count` 会保存为 `followers_count` 并导入 `author_followers_count`，`url_token` 会保存为 `author_profile_url`；时间统一为 Asia/Shanghai；`zvideo` 记录会按视频跳过。B站正式调度走专栏/图文 article 搜索，`image_urls`、`pubdate/pub_time`、作者昵称/ID、点赞/评论/浏览数会结构化入库；单个 Opus 页面只作为定向证据。如果后续 JSONL 中出现视频记录，会计入 `skipped_video` 并跳过入库。

导入字段映射：

| 目标字段 | 来源字段 |
|---|---|
| `platform_key` | 运行平台：`bilibili`、`xhs`、`weibo`、`douyin`、`zhihu` |
| `platform_post_id` | `note_id`、`aweme_id`、`content_id`、`id` 等非视频内容 ID |
| `canonical_url` | `note_url`、`aweme_url`、`content_url`、`url`、`share_url`，缺失时按平台 ID 拼接 |
| `title` | `title` |
| `content_text` | 优先 `content_text` 或 `content`，其次 `desc`，最后 `title` |
| `author_display_name` | `nickname` 或 `user_nickname` |
| `author_platform_id` | 小红书 `user_id`、`creator_hash` 或其他平台用户 ID |
| `author_followers_count` | 微博 `followers_count/fans_count`，小红书作者主页补充字段 `fans_count`、`followers_count` 或 `fans`，知乎搜索结果 `author.follower_count` 归一后的 `followers_count` |
| `published_at` | 发帖时间，统一保存为 Asia/Shanghai ISO 字符串，如 `2024-04-06T15:35:00+08:00`。优先取平台原始发布时间字段，如 `create_time`、`publish_time`、`time`、`datePublished`；`captured_at` 只表示本项目抓取时间 |
| `city_name` | 从检索关键词匹配山东 16 市名称或别名，如 `济南旅游`、`烟台旅游` 分别写入 `济南市`、`烟台市`；不从正文内容反推城市 |
| `post_likes_count` | `liked_count`、知乎 `voteup_count` |
| `post_favorites_count` | `collected_count` 等收藏字段 |
| `post_comments_count` | `comment_count`、`comments_count` 等评论字段 |
| `post_shares_count` | `share_count`、`shared_count` 等分享字段 |
| `post_views_count` | `view_count`、`play_count` 等浏览字段 |
| `web_post_images` | `cover`、`image`、`pic`、`image_list`、`image_urls`、`avatar` 等 URL 字段；微博来自 `mblog.pics` 保存后的 `image_list`，知乎来自正文 HTML 保存后的 `image_list`，B站 article 搜索来自 `image_urls` |
| `raw_sample_json` | MediaCrawler 原始 JSONL 行 |

代码只在导入边界识别不同平台对同类指标的字段名差异，内部持久化结构统一写入 `web_posts` / `web_post_images`。视频记录只用于识别和跳过，不进入内容主表。

运行命令：

正式调度调用执行器后，摘要同时包含正式校验和入库结果：

```json
{
  "formal_validation": {
    "candidate_count": 80,
    "valid_new_count": 50,
    "valid_existing_count": 15,
    "new_target_met": true,
    "stop_reason": "target_new_met"
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

B站 Opus 详情页、携程、去哪儿、豆瓣小组由 `ctf_resource_crawl.py` 保存页面证据，再由 `import_ctf_captures.py` 同步写入两层数据：`ctf_captures` / `ctf_capture_images` 作为证据和调试底座，`web_posts` / `web_post_images` 作为用户使用的统一内容主表。页面级抓取会从 `article:published_time`、JSON-LD、`time[datetime]` 等明确页面元数据中提取 `published_at`，并在抓取元数据中直接保存为 Asia/Shanghai ISO 字符串；B站 Opus/图文页还会从可见文本中的明确日期行提取。没有明确证据时保持为空，不用抓取时间替代。截图属于证据附件，截图失败会记录到 `artifact_errors`，但只要页面内容、文本和图片资源已成功采集，不应把整条内容标成抓取失败。

去哪儿旧游记 URL 可能跳转到 `travelbook/note/...` 后返回平台错误页，页面可见文本为“非常抱歉，您访问的页面不存在，可能已被删除 / 返回上一页”。这类页面可能仍然出现 `ok=true`、`content_ready=true`，因此不能只靠导航状态判断是否生成用户可见记录。`import_ctf_captures.py` 会在导入边界通过 `looks_like_error_page()` 识别 `site_key=qunar` 的错误页标记：原始 `ctf_captures` 证据继续保留，便于排查目标失效和页面跳转；但不生成 `web_posts`，避免把错误页正文当作有效图文内容。历史上已导入的错误记录应通过受控终端 SQL 或维护脚本备份后清理，不从管理端前端直接删除。

豆瓣小组入库有四处平台特定逻辑：`extract_douban_topic_fields()` 从话题页 `rendered.html` 提取标题、作者五元组（display_name/platform_id/profile_url/avatar_url + group_name 写入 `author_description`）、发帖时间（`create-time`）、正文（`link-report`）；`is_douban_topic_url()` 保证只有 `/group/topic/{id}/` 详情页进 `web_posts`，搜索页只留 `ctf_captures` 证据层；`city_name_from_keyword()` 把 `crawl_targets.json` 传入的 keyword（经 `crawl_runner.py --keyword` → `capture_meta`）归一成山东 16 地市标准名写入 `city_name`；作者粉丝量不在话题页入库时填入，而由独立 enrichment 脚本访问 people 页补全（见 `docs/platforms/page-evidence.md`）。`author_followers_count` / `author_following_count` / `author_posts_count` 在话题入库时为 NULL，粉丝量在 enrichment 后按 `author_platform_id` 批量 UPDATE；隐私用户（people 页显示「由于用户的设置，无法查看主页内容」）保持 NULL 并标 `followers_source="privacy_restricted"`，不当作 0。成功且内容就绪的页面正文来自 `visible_text.txt`，图片来自 `images.json`。

显式抓取豆瓣 topic URL 时不能只用 `--urls --site-label douban_group`，否则会按 URL 创建隔离 profile，无法复用 `ctf_login_warmup.py` 写入的 `data/browser_profiles_ctf/douban_group/` 登录态；应同时传 `--configured-site-urls`。调度器的页面级任务会自动用这个方式传 `target_url`。

单次抓取：

```bash
.venv/bin/python scripts/ctf_resource_crawl.py \
  --sites bilibili \
  --headless \
  --max-image-save 3 \
  --max-scrolls 2
```

导入指定产物：

```bash
.venv/bin/python scripts/import_ctf_captures.py \
  --capture-meta outputs/ctf_resource_crawls/<批次>/<目标>/capture_meta.json
```

调度器正常执行 `ctf_resource_crawl` 时会自动导入，除非传入 `--no-import`。

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

查看图片 URL：

```sql
SELECT
  p.platform_key,
  p.platform_post_id,
  i.image_role,
  i.image_url
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
- 平台已知错误页要在生成 `web_posts` 前过滤；例如去哪儿“页面不存在，可能已被删除”只保留 `ctf_captures` 证据，不进入用户内容主表。
- flag-like 文本只做格式校验，不在抓取阶段清洗。
- `skipped=true` 的视频跳过产物只保留为运行证据，不导入 `ctf_captures`，也不生成 `web_posts`。

MediaCrawler 入库采用去重更新：

- 优先用 `(platform_key, platform_post_id)` 匹配旧记录。
- 没有平台 ID 时用 `(platform_key, canonical_url)` 匹配旧记录。
- 更新帖子时会重建该帖子的 `web_post_images` 行。
- 原始 JSONL 行完整保留在 `raw_sample_json`，便于后续清洗补字段。

## 手工验证步骤

用临时库验证，不污染默认库：

```bash
source .venv/bin/activate
python scripts/mediacrawler_crawl.py \
  --platforms weibo \
  --keyword 济南旅游 \
  --candidate-hard-limit 20 \
  --target-new-posts 1 \
  --timeout-per-platform 180 \
  --db temp/mediacrawler_import_verify.sqlite
```

查询结果：

```bash
sqlite3 temp/mediacrawler_import_verify.sqlite \
  "select platform_key,count(*) from web_posts group by platform_key;"
```

显式验证调度同步：

```bash
.venv/bin/python scripts/crawl_runner.py \
  --db temp/scheduler_verify.sqlite \
  --sync-only
```

查看计划命令：

```bash
.venv/bin/python scripts/crawl_runner.py \
  --db temp/scheduler_verify.sqlite \
  --dry-run \
  --max-jobs 5
```

## 目前边界

- 当前规则禁止视频功能。项目侧 `--get-media` 会直接失败；MediaCrawler 默认 `--get_media false`，只有小红书 `--download-images` 会开启图片样本保存，视频保存分支仍被禁用。
- MediaCrawler 会导入封面、头像等图片 URL；明确视频记录会被跳过并计入 `skipped_video`，不作为失败入库。
- 知乎当前是 MediaCrawler 入库路径；页面级产物只作为临时排障证据，不作为默认调度链路。
- 正式有效性过滤在入库前完成；业务清洗、低质量分级和 flag-like 误报处理仍放在 SQL 视图或下游清洗层。
- `outputs/` 不是长期主存储。结构化内容、作者、互动数、URL、状态和摘要应进入 SQLite；文件系统只长期保留图片、截图、必要日志和短期 staging。
- `temp/` 不参与正式入库和调度，只保存临时验证数据库或一次性测试结果。代码不能依赖 `temp/` 中已有文件。
