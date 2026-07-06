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

`scripts/db_bootstrap.py` 是统一入口。`crawl_runner.py`、MediaCrawler 入库和 CTF artifact 导入都会自动执行 bootstrap，补齐 schema、`source_platforms` 和 `crawl_jobs` 配置；手工 `--sync-only` 只用于显式刷新或排查。

`web_posts` 是统一内容主表，面向用户查询和后续数据使用。`ctf_captures` 是证据和调试底座，面向程序脚本或 Agent 排查抓取过程。页面级抓取成功后，也会归一化生成 `web_posts` 行，并通过 `web_posts.source_capture_id` 关联对应 `ctf_captures.id`。

当前结构不再保留“只入 `ctf_captures`、不入 `web_posts`”的内容形态。已有成功且内容就绪的页面级证据记录，应通过 `import_ctf_captures.py` 重新导入或同步，使用户查询统一落在 `web_posts` 上。

## MediaCrawler 结果入库

微博、小红书、抖音、知乎等非视频结构化结果由 `scripts/mediacrawler_crawl.py` 调用 MediaCrawler 后导入 `web_posts`。小红书搜索会在低样本量下补拉作者主页指标，用于填充粉丝数等作者字段。知乎搜索结果中的回答/文章 `created_time` 会映射到 `published_at`，并统一为 Asia/Shanghai；`zvideo` 记录会按视频跳过。B站默认走页面级 Opus/图文证据抓取；手工运行 B站 MediaCrawler 时，如果返回视频记录，会计入 `skipped_video` 并跳过入库。

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
| `author_followers_count` | 小红书作者主页补充字段 `fans_count`、`followers_count` 或 `fans` |
| `published_at` | 发帖时间，统一保存为 Asia/Shanghai ISO 字符串，如 `2024-04-06T15:35:00+08:00`。优先取平台原始发布时间字段，如 `create_time`、`publish_time`、`time`、`datePublished`；`captured_at` 只表示本项目抓取时间 |
| `post_likes_count` | `liked_count`、知乎 `voteup_count` |
| `post_favorites_count` | `collected_count` 等收藏字段 |
| `post_comments_count` | `comment_count`、`comments_count` 等评论字段 |
| `post_shares_count` | `share_count`、`shared_count` 等分享字段 |
| `post_views_count` | `view_count`、`play_count` 等浏览字段 |
| `web_post_images` | `cover`、`image`、`pic`、`avatar` 等 URL 字段 |
| `raw_sample_json` | MediaCrawler 原始 JSONL 行 |

代码只在导入边界识别不同平台对同类指标的字段名差异，内部持久化结构统一写入 `web_posts` / `web_post_images`。视频记录只用于识别和跳过，不进入内容主表。

运行命令：

```bash
.venv/bin/python scripts/mediacrawler_crawl.py \
  --platforms xhs weibo douyin zhihu \
  --keyword 济南旅游 \
  --timeout-per-platform 180
```

输出摘要会包含入库结果：

```json
{
  "import_result": {
    "db": "data/trippostcollect.sqlite",
    "jsonl_files": 3,
      "imported": 34,
      "skipped": 2,
      "skipped_video": 2,
      "parse_errors": 0
  }
}
```

## 页面级抓取结果入库

B站、携程、去哪儿、穷游、豆瓣小组由 `ctf_resource_crawl.py` 保存页面证据，再由 `import_ctf_captures.py` 同步写入两层数据：`ctf_captures` / `ctf_capture_images` 作为证据和调试底座，`web_posts` / `web_post_images` 作为用户使用的统一内容主表。页面级抓取会从 `article:published_time`、JSON-LD、`time[datetime]` 等明确页面元数据中提取 `published_at`；B站 Opus/图文页还会从可见文本中的明确日期行提取。没有明确证据时保持为空，不用抓取时间替代。成功且内容就绪的页面正文来自 `visible_text.txt`，图片来自 `images.json`。

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
.venv/bin/python scripts/mediacrawler_crawl.py \
  --platforms weibo \
  --keyword 济南旅游 \
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
- 清洗和过滤不在抓取阶段做，后续如需去重、筛选低质量内容或处理 flag-like 误报，应在 SQL 视图或清洗脚本中完成。
- `outputs/` 不是长期主存储。结构化内容、作者、互动数、URL、状态和摘要应进入 SQLite；文件系统只长期保留图片、截图、必要日志和短期 staging。
- `temp/` 不参与正式入库和调度，只保存临时验证数据库或一次性测试结果。代码不能依赖 `temp/` 中已有文件。
