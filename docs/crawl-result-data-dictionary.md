# 抓取结果数据字典

本文描述面向研究和数据使用的现行抓取结果字段。它不记录数据库即时行数，也不包含调度、登录、
checkpoint、失败恢复或迁移字段；实时规模应直接查询 SQLite。正式入库、事务和媒体校验规则见
[数据持久化](data-persistence.md)。

## 数据模型

用户使用的结果由两张表承载：

| 表 | 粒度 | 用途 |
|---|---|---|
| `web_posts` | 每篇帖子一行 | 帖子、作者、正文和互动指标 |
| `web_post_images` | 每张正文图片一行 | 权威正文图片、本地文件和字节校验信息 |

数据库目前没有独立作者表。同一作者发布多篇帖子时，作者字段重复保存在对应 `web_posts` 行中。
平台内作者关联键为 `platform_key + author_platform_id`；帖子与图片通过
`web_posts.id = web_post_images.web_post_id` 关联。

## `web_posts` 帖子字段

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| `id` | INTEGER | 否 | 数据库内部帖子主键 |
| `platform_key` | TEXT | 否 | `bilibili`、`douyin`、`weibo`、`xhs` 或 `zhihu` |
| `platform_post_id` | TEXT | 是 | 平台原始帖子 ID |
| `source_type` | TEXT | 否 | 内容来源或平台内容类型 |
| `source_url` | TEXT | 否 | 抓取输入或原始来源 URL |
| `canonical_url` | TEXT | 是 | 规范化帖子 URL |
| `title` | TEXT | 是 | 平台标题；没有独立标题的平台可以为空 |
| `published_at` | TEXT | 是 | 平台原始发帖时间，保存为 Asia/Shanghai ISO |
| `keyword` | TEXT | 是 | 发现该帖时实际使用的检索词 |
| `content_text` | TEXT | 是 | 权威详情页或详情接口取得的正文 |
| `content_length` | INTEGER | 否 | `content_text` 的字符长度 |
| `post_images_count` | INTEGER | 否 | 关联的权威正文图片数量 |

帖子稳定唯一性优先使用 `platform_key + platform_post_id`；平台 ID 不可用时使用
`platform_key + canonical_url`。标题、搜索摘要或预览文本不能单独作为完整正文入库。

## `web_posts` 作者字段

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| `author_display_name` | TEXT | 是 | 作者昵称或展示名称 |
| `author_platform_id` | TEXT | 是 | 平台作者 ID；只在同一平台命名空间内关联 |
| `author_profile_url` | TEXT | 是 | 作者主页 URL |
| `author_description` | TEXT | 是 | 作者简介或主页描述 |
| `author_followers_count` | INTEGER | 是 | 抓取时观察到的粉丝数；真实 0 有效 |
| `author_following_count` | INTEGER | 是 | 作者关注数 |
| `author_posts_count` | INTEGER | 是 | 平台展示的作者发帖或作品总数 |
| `author_platform_level` | TEXT | 是 | 平台等级 |
| `author_verified` | INTEGER | 是 | 规范化认证布尔值 |
| `author_verified_text` | TEXT | 是 | 平台认证说明原文 |
| `author_json` | TEXT/JSON | 否 | 作者结构化补充字段 |

粉丝数缺失必须保存为 `NULL`，不能用 0 代替；真实 0 必须同时具有
`author_json.followers_observed=true` 和非空 `followers_source`。跨平台同名作者不能自动合并。

作者头像不属于抓取结果：不采集、不下载、不持久化，不得出现在项目 JSON/JSONL、摘要、日志、
SQLite 字段或图片关系中，也不保留 URL-only 参考关系。

### `author_json` 子字段

| JSON 键 | 类型 | 含义 |
|---|---|---|
| `creator_hash` | integer/text | 平台作者原始 ID 或归一化作者键；导出时按字符串处理 |
| `nickname` | text | 作者昵称 |
| `followers_count` | integer | 与顶层 `author_followers_count` 对应 |
| `followers_observed` | boolean | 是否真实观察到粉丝数 |
| `followers_source` | text | 粉丝数的平台来源或采集来源 |
| `following_count` | integer/null | 作者关注数 |
| `posts_count` | integer/null | 作者作品或发帖总数 |
| `liked_count` | integer/null | 平台展示的作者累计获赞等作者级指标 |

顶层作者列用于 SQL 查询，`author_json` 保留观察状态、来源和平台补充信息。两者冲突时必须核对
导入记录，不能任意选择其一。

## `web_posts` 互动指标

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| `post_likes_count` | INTEGER | 是 | 点赞或赞同数 |
| `post_favorites_count` | INTEGER | 是 | 收藏数 |
| `post_comments_count` | INTEGER | 是 | 评论数量；不包含评论正文 |
| `post_shares_count` | INTEGER | 是 | 分享数量 |
| `post_reposts_count` | INTEGER | 是 | 转发数量 |
| `post_views_count` | INTEGER | 是 | 浏览、阅读或播放展示数 |
| `metrics_json` | TEXT/JSON | 否 | 互动指标结构化对象 |

`metrics_json` 使用 `liked_count`、`favorites_count`、`comments_count`、`shares_count`、
`reposts_count` 和 `views_count` 保存相应平台值。`NULL` 表示平台未提供或当前没有提取，不能解释为
0；评论字段只保存评论总数。

## `web_post_images` 正文图片字段

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| `id` | INTEGER | 否 | 数据库内部图片主键 |
| `web_post_id` | INTEGER | 否 | 关联 `web_posts.id` 的外键 |
| `image_index` | INTEGER | 否 | 帖内图片顺序，从 0 连续编号 |
| `image_url` | TEXT | 否 | 平台权威正文图片 URL |
| `image_role` | TEXT | 否 | 现行正式结果只允许 `content` |
| `local_path` | TEXT | 是 | 项目内本地文件路径；正式新记录必须存在 |
| `width` | INTEGER | 是 | 本地复验得到的图片宽度 |
| `height` | INTEGER | 是 | 本地复验得到的图片高度 |
| `mime_type` | TEXT | 是 | 本地文件真实 MIME 类型 |
| `sha256` | TEXT | 是 | 本地文件 SHA-256 |
| `raw_image_json` | TEXT/JSON | 是 | 图片来源与字节复验证据 |

`web_post_id + image_index` 具有唯一性。正文图片不包含作者头像、作者主页图片、视频、音乐、公式图、
封面或仅用于搜索预览的图片。

## 过程字段边界

以下内容存在于控制面或证据表，但不属于面向研究使用的抓取结果字段：

- 调度、运行和重试：`crawl_jobs`、`crawl_attempts`、`crawl_run_reports`、`collection_batches`；
- 发现前沿和候选记忆：`crawl_discovery_*`、`xhs_discovery_*`；
- 小红书账号控制面：`xhs_accounts`、`xhs_account_leases`、`xhs_account_events`、`xhs_runs`；
- 页面调试证据：`ctf_captures`、`ctf_capture_images`；
- 登录健康、迁移和历史排除：`profile_health_checks`、`schema_migrations`、
  `historical_image_exclusions`；
- `web_posts` 的 `captured_at`、`raw_sample_json`、`artifact_dir`、`capture_method`、`status`、
  `created_at`、`updated_at`、`source_capture_id`；
- `web_post_images` 的 `created_at`。

## 实时规模查询

文档不冻结会随抓取变化的数量。需要当前规模时直接查询：

```sql
SELECT platform_key, COUNT(*) AS posts
FROM web_posts
GROUP BY platform_key
ORDER BY platform_key;

SELECT COUNT(*) AS content_images
FROM web_post_images
WHERE image_role = 'content';
```

结果查询以 `web_posts` 为主，图片通过 `web_post_images` 关联；视频记录不进入结果主表，
`published_at` 不得使用抓取时间代替。
