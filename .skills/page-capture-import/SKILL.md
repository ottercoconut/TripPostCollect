---
name: page-capture-import
description: 仅用于 TripPostCollect 面向 bilibili、ctrip、qunar、douban_group 等非 MediaCrawler 目标的页面级证据抓取、反自动化行为、浏览器韧性和导入流程。
---

# 页面证据抓取与导入

## 触发条件

- 用户要求抓取、验证、导入或调试页面级证据抓取。
- 改动涉及 `scripts/ctf_resource_crawl.py`、`scripts/import_ctf_captures.py`、`scripts/web_sites.py` 或 `ctf_resource_crawl` 任务。
- 需要分析 `capture_meta.json`、截图、HTML、可见文本或图片资源问题。

## 输入材料

- `docs/crawl-architecture.md`
- `docs/anti-automation-behavior.md`
- `docs/data-persistence.md`
- `config/crawl_targets.json`
- `scripts/ctf_resource_crawl.py`
- `scripts/import_ctf_captures.py`
- `scripts/ctf_login_warmup.py`
- `scripts/web_sites.py`

## 工作流程

1. 确认站点已配置为 `ctf_resource_crawl`，并在 `scripts/web_sites.py` 中识别站点策略。
2. 修改浏览器上下文、浏览器配置目录、节流、行为配置、Scrapling 预检、冷却或可见/无界面行为前，先读 `docs/anti-automation-behavior.md`。
3. 除非用户要求更广泛的证据，否则用较小的图片和滚动限制做定向抓取。
4. 检查 `capture_meta.json`、`summary.json`、`visible_text.txt` 摘要、图片数量、`policy_events`、`behavior_events`、浏览器引擎和失败分类。
5. 需要时用 `scripts/import_ctf_captures.py` 导入明确的 `capture_meta.json` 文件；验证代码改动时使用临时数据库。
6. 验证两层数据：`ctf_captures` 中的证据行，以及 `web_posts` 中面向用户的行。
7. 发帖时间提取逻辑变化时，确认 `published_at` 来自页面元数据或可见日期文本，而不是抓取时间。

## 豆瓣小组特定逻辑

豆瓣小组在通用页面级流程之上有五处平台特定逻辑，改动时需整体对照 `docs/crawl-architecture.md` 豆瓣小组节：

- **登录**：cookie 标记是 `dbcl2`（末尾带 2，非 `dbcl`）；浏览器配置目录在 `data/browser_profiles_ctf/douban_group/`，由 `ctf_login_warmup.py` 预热。
- **显式 URL**：豆瓣 topic URL 需要同时传 `--site-label douban_group --configured-site-urls`，否则 `--urls` 会使用按 URL 派生的隔离 profile，拿不到 `data/browser_profiles_ctf/douban_group/` 登录态。
- **话题字段提取**：`import_ctf_captures.py` 的 `extract_douban_topic_fields()` 从 `rendered.html` 提取标题/作者五元组/发帖时间(`create-time`)/正文(`link-report`)。
- **搜索页过滤**：`is_douban_topic_url()` 只让 `/group/topic/{id}/` 进 `web_posts`，搜索页只留证据层。
- **keyword→城市**：`crawl_runner.py` 传 `--keyword`，`city_name_from_keyword()` 归一山东 16 市写入 `city_name`。
- **作者粉丝量**：话题页不含粉丝数，需独立访问 people 页 `www.douban.com/people/{id}/`，取 `<p class="rev-link">...被{N}人关注</p>`。隐私用户（「由于用户的设置，无法查看主页内容」）`rev-link` 块消失，`author_followers_count` 保持 NULL 标 `followers_source="privacy_restricted"`，**NULL≠0**。people 页 JS 里的「异常状态」是所有页面共有的失败提示文案，非用户状态信号。

## 验证

- Python 改动：`.venv/bin/python -m py_compile scripts/ctf_resource_crawl.py scripts/import_ctf_captures.py`
- 抓取证据：`outputs/ctf_resource_crawls/` 下的 `summary.json` 和目标 `capture_meta.json`。
- 导入证据：SQL 查询显示 `ctf_captures` 行以及对应的 `web_posts.source_capture_id`。
- 字段变化：只有具备产物证据时才更新 `docs/platform-field-coverage.md`。

## 常见错误

- 在没有导入器支持时，把页面级证据当作完整结构化字段。
- 把已跳过的抓取作为面向用户的帖子导入。
- 可见文本、元数据和摘要已经足够时，仍读取完整渲染 HTML。
- 把 `temp/` 产物当作稳定项目状态来依赖。
- 没有任务特定理由时，在正式取证中使用 `--no-throttle` 或无界面模式。
- 豆瓣显式 topic URL 只传 `--urls --site-label douban_group`，导致抓取落到 `douban_group_<digest>` profile 并丢登录态。
- 把豆瓣隐私用户的粉丝量 NULL 当作 0；`rev-link` 块消失是隐私标记，需标 `followers_source="privacy_restricted"` 保持 NULL。

## 参考文件

- `docs/crawl-architecture.md`
- `docs/anti-automation-behavior.md`
- `docs/data-persistence.md`
- `scripts/ctf_resource_crawl.py`
- `scripts/import_ctf_captures.py`
- `scripts/web_sites.py`

## 脚本

- 现有：`scripts/ctf_resource_crawl.py`
- 现有：`scripts/import_ctf_captures.py`
- 现有：`scripts/ctf_login_warmup.py`（豆瓣登录预热，检测 `dbcl2`）
- 计划：`scripts/ctf_author_enrich.py`（豆瓣作者粉丝量补全，访问 people 页，按 `author_platform_id` 去重 UPDATE）
