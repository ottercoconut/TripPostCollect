# 抓取架构

## 总体结构

当前结构以“调度器 + MediaCrawler + 通用兜底抓取”为主：

```text
config/crawl_targets.json
  -> scripts/crawl_runner.py
      -> scripts/mediacrawler_crawl.py
          -> tools/MediaCrawler
          -> web_posts / web_post_images
      -> scripts/ctf_resource_crawl.py
          -> outputs/ctf_resource_crawls
          -> import_ctf_captures.py
          -> ctf_captures / ctf_capture_images
          -> web_posts / web_post_images
```

项目不再维护 B站、微博、小红书、抖音的全量自写采集逻辑。项目侧只保留三类能力：

- 调度：长期运行、重试、摘要、任务启停。
- 对接：调用 MediaCrawler，并把输出映射到本地库。
- 兜底：对 MediaCrawler 不覆盖的平台做页面证据和图片资源抓取，并把成功页面归一化到统一内容表。

数据层分工固定为：`web_posts` 是用户使用的统一内容主表；`ctf_captures` 是程序和 Agent 使用的证据/调试底座。

## 平台分工

| 平台 | 任务类型 | 入口 | 入库表 |
|---|---|---|---|
| B站 | `ctf_resource_crawl`；手工 article 搜索 | `ctf_resource_crawl.py --sites bilibili`；`mediacrawler_crawl.py --platforms bilibili` | 页面级：`web_posts`、`web_post_images`、`ctf_captures`、`ctf_capture_images`；article 搜索：`web_posts`、`web_post_images` |
| 微博 | `mediacrawler_search` | `mediacrawler_crawl.py --platforms weibo` | `web_posts`、`web_post_images` |
| 小红书 | `mediacrawler_search` | `mediacrawler_crawl.py --platforms xhs` | `web_posts`、`web_post_images` |
| 抖音 | `mediacrawler_search` | `mediacrawler_crawl.py --platforms douyin` | `web_posts`、`web_post_images` |
| 知乎 | `mediacrawler_search` | `mediacrawler_crawl.py --platforms zhihu` | `web_posts`、`web_post_images` |
| 携程 | `ctf_resource_crawl` | `ctf_resource_crawl.py --sites ctrip` | `web_posts`、`web_post_images`、`ctf_captures`、`ctf_capture_images` |
| 去哪儿 | `ctf_resource_crawl` | `ctf_resource_crawl.py --sites qunar` | `web_posts`、`web_post_images`、`ctf_captures`、`ctf_capture_images` |
| 豆瓣小组 | `ctf_resource_crawl` | `ctf_resource_crawl.py --sites douban_group`；显式话题 URL 用 `--urls ... --site-label douban_group --configured-site-urls` | `web_posts`、`web_post_images`、`ctf_captures`、`ctf_capture_images` |

知乎默认走 MediaCrawler 搜索链路。该链路依赖可用 Chrome/CDP 和登录态；如果登录态失效，应先用 `mediacrawler_login_warmup.py --platforms zhihu` 刷新，再运行调度。登录 warmup 会使用与 MediaCrawler CDP 相同的浏览器可执行文件和 `tools/MediaCrawler/browser_data/zhihu_user_data_dir`，登录成功后默认关闭并重开一次验证持久化是否生效，并把已验证的 `d_c0/z_c0` cookie 保存到本地快照 `trippostcollect_cookie_snapshot.json`。`mediacrawler_crawl.py --platforms zhihu` 会优先读取该快照并通过环境变量注入 MediaCrawler；如果快照缺失或不含必需 cookie，则直接早停，不再启动知乎抓取浏览器。项目 wrapper 会给知乎设置 `TRIPPOSTCOLLECT_CLEAN_BROWSER_TABS=1` 和 `TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS=8`：MediaCrawler 启动 Chrome 前会删除 profile 中的 Session/Last Tabs 会话恢复文件，连接后只保留本次运行页；随后在 cookie 注入和页面 reload 后等待登录态稳定，并选用最新知乎页继续创建 API client，减少历史标签页和登录态未稳定造成的重复登录流程。

小红书也依赖 MediaCrawler 搜索链路，但登录态处理不同。实测中，小红书扫码成功后运行中的 CDP context 能看到登录 UI 和 `web_session/a1/webId/gid`，关闭窗口后 Chromium profile 的 `Default/Cookies` 仍可能为空，导致下次新窗口再次扫码。因此项目侧把小红书登录态保存为 `tools/MediaCrawler/browser_data/xhs_user_data_dir/trippostcollect_storage_state.json`，其中包含 cookies、localStorage 和 sessionStorage。统一刷新入口是 `mediacrawler_login_warmup.py --platforms xhs`：脚本在 headed 窗口等待人工完成安全确认/扫码，只认左侧“我”入口为成功，随后关闭并重开验证持久化，验证成功后写入 storage snapshot。`mediacrawler_crawl.py --platforms xhs` 会显式读取同一 snapshot；默认 `cookie` 模式下 snapshot 缺失会早停并提示先运行 warmup。`mediacrawler_batch_validate.py` 的小红书 preflight 只作为批量验证前的恢复检查和截图证据，不再作为主登录刷新入口。

微博默认走 MediaCrawler 搜索链路。为了满足图文和作者粉丝量校验，项目侧修正了 `tools/MediaCrawler/store/weibo/__init__.py` 的保存字段：从 `mblog.user` 保留 `followers_count/fans_count`，从 `mblog.pics` 保留 `image_list/image_count`，但仍不下载媒体文件。2026-07-06 的验证结果见 `outputs/mediacrawler_runs/20260706T151935+0000/weibo/usable_50_validation.json`：149 条候选中可筛出 50 条唯一图文，图片 URL、粉丝量、正文、发布时间和互动指标均完整。

B站长期调度默认仍走 Opus/图文详情页的页面级抓取。手工关键词验证时，`mediacrawler_crawl.py --platforms bilibili` 不调用 MediaCrawler 的 B站视频搜索，而是走 B站专栏/图文 article 搜索接口，写出 `bili/jsonl/search_contents_*.jsonl` 后复用统一入库逻辑。2026-07-08 的 `烟台旅游` 验证见 `outputs/mediacrawler_runs/20260707T173332+0000/summary.json`：50 条唯一 article 图文全部入库，图片 URL、发布时间、作者昵称/ID、点赞/评论/浏览指标均结构化，视频记录为 0。

## 调度配置

长期任务只从 `config/crawl_targets.json` 生成。当前只使用两种 `job_kind`：

```text
mediacrawler_search
ctf_resource_crawl
```

数据库 bootstrap 会把旧版 `crawl_jobs` 迁移到当前两类 job_kind；不在当前配置中的历史任务会被标记为 `disabled`。

常用调度命令：

```bash
.venv/bin/python scripts/crawl_runner.py --sync-only
.venv/bin/python scripts/crawl_runner.py --dry-run --max-jobs 5
.venv/bin/python scripts/crawl_runner.py --max-jobs 3
.venv/bin/python scripts/crawl_runner.py --job-key mc_zhihu_jinan_search --max-jobs 1
```

`crawl_runner.py`、MediaCrawler 入库入口和 CTF artifact 导入入口都会自动补齐 schema、平台注册表和配置任务；`--sync-only` 主要用于显式刷新和检查。

## MediaCrawler 对接

`scripts/mediacrawler_crawl.py` 做四件事：

1. 按平台把项目名映射到 MediaCrawler 参数：
   - `bilibili` -> `bili`
   - `xhs` -> `xhs`
   - `weibo` -> `wb`
   - `douyin` -> `dy`
   - `zhihu` -> `zhihu`
2. 以低并发方式调用 MediaCrawler：
   - `--max_concurrency_num 1`
   - 默认不下载媒体文件：`--get_media false`
   - 默认走 cookie 登录方式：`--lt cookie`
   - 微博保存搜索结果里的 `mblog.pics` 图片 URL 和 `mblog.user.followers_count/followers_count_str`，用于图文完整性和作者粉丝量校验；不下载图片文件。
   - B站 `--platforms bilibili` 手工分支使用 article 搜索，只保存非视频专栏/图文结果；`image_urls`、`pubdate/pub_time`、作者昵称/ID、点赞/评论/浏览数会映射入库。
   - 小红书优先开启可见浏览器/CDP，启动时恢复 `trippostcollect_storage_state.json`，并在搜索结果后补拉作者主页指标；搜索结果含视频时会继续翻页补足图文记录。
   - 知乎开启 CDP 模式，启动前清理历史标签页恢复文件，搜索结果中的回答/文章 `created_time` 会映射到 `published_at`；正文 HTML 在清洗为纯文本前提取图片 URL 为 `image_list/image_count`；登录态从本地 cookie 快照注入，`zvideo` 记录按视频跳过。
   - `published_at` 入库时统一保存为 Asia/Shanghai ISO 字符串；运行审计类 `captured_at` 仍保留 UTC。
   - 配置中的 `download_images: true` 只会在小红书任务上翻译为项目侧 `--download-images`；视频保存分支仍被禁用。
3. 统计 JSONL 内容记录、评论记录、作者字段、图片文件和异常视频文件数量。
4. 默认把非视频 JSONL 内容导入 `web_posts` 和 `web_post_images`，并写入平台原始发帖时间 `published_at`；明确视频记录只计入 `skipped_video`，不入库。

B站默认不走 MediaCrawler 视频搜索调度，因为搜索结果容易返回视频。`bilibili` 参数用于手工 article 图文搜索验证；如果后续 JSONL 中出现明确视频记录，导入阶段仍会跳过。

单平台命令：

```bash
.venv/bin/python scripts/mediacrawler_crawl.py \
  --platforms xhs \
  --keyword 济南旅游 \
  --mediacrawler-max-notes 3 \
  --login-type qrcode \
  --headed \
  --download-images \
  --timeout-per-platform 420
```

只保存文件、不入库：

```bash
.venv/bin/python scripts/mediacrawler_crawl.py \
  --platforms xhs \
  --keyword 济南旅游 \
  --no-import
```

`--get-media` 已禁用。当前规则只允许图文内容和图片证据；小红书图片样本通过 `--download-images` 单独开启。视频目标、视频媒体请求和 MediaCrawler 返回的视频记录都会被忽略或跳过，不计为抓取失败。

## 页面级兜底抓取

`scripts/ctf_resource_crawl.py` 用于 MediaCrawler 不覆盖或默认搜索不适合当前图文规则的平台。它保存：

- `rendered.html`
- `visible_text.txt`
- `screen.png`
- `images.json`
- `failed_images.json`
- `capture_meta.json`

页面级抓取会从明确页面元数据提取 `published_at`，例如 `article:published_time`、JSON-LD 的 `datePublished`、`time[datetime]`。提取不到时保持为空，不用抓取时间冒充发帖时间。

抖音页面级兜底使用 `commit` 短导航加内容就绪判断，不再只依赖 `domcontentloaded`。当 `body` 文本、`#root`、图片或 SSR 数据出现时即可判定页面有可用内容。

页面级抓取会安装无视频策略：导航前跳过明显视频 URL，运行时不处理 `media` 资源、视频后缀、m3u8/mpd 等请求，并在 `capture_meta.json` 的 `media_policy` 中记录跳过或不处理计数。

页面级抓取还会过滤非内容图片：`is_image_response` 排除 `application/*`、`text/*` 等 content_type 的响应（例如腾讯验证码监控 `turing.captcha.qcloud.com/cap_monitor` 返回的 JSON），并在图片响应处理处跳过已知装饰图 URL（当前为豆瓣 `doubanio.com/f/` 下的 CSS 资源，域名限定只影响豆瓣）。这些资源不记录、不下载、不占 `max_image_save` 配额。

显式 URL 默认仍按临时目标运行，会使用按 URL 派生的独立 profile；需要复用已配置站点策略、站点 profile、移动端上下文和浏览器引擎时，必须同时传 `--site-label <site_key>` 和 `--configured-site-urls`。调度器的 `ctf_resource_crawl` 任务会把 `crawl_targets.json` 中的 `target_url` 按这个方式传给抓取脚本，因此页面级任务不会再忽略配置里的目标 URL。

### 豆瓣小组

豆瓣小组走页面级兜底链路且需要登录态（`web_sites.py` 中 `login_required=True`）。整条链路分四段：登录预热、话题字段提取、搜索页过滤与城市映射、作者粉丝量补全。

**登录预热**：`scripts/ctf_login_warmup.py` 打开与 `ctf_resource_crawl.py` 相同的持久化 profile `data/browser_profiles_ctf/douban_group/`，在 headed 窗口里人工完成登录，轮询检测 cookie **`dbcl2`**（末尾带 `2`，HttpOnly 持久 cookie；早期文档误记为 `dbcl`）命中即视为登录成功，随后默认关闭并重开一次验证持久化是否生效，并把已验证 cookie 写入 profile 目录下的 `trippostcollect_cookie_snapshot.json`。`ctf_resource_crawl.py --sites douban_group` 复用同一 profile；显式 topic URL 需要使用 `--urls ... --site-label douban_group --configured-site-urls` 才会复用同一 profile。刷新豆瓣登录态：

```bash
.venv/bin/python scripts/ctf_login_warmup.py --sites douban_group
```

**话题字段提取**：`import_ctf_captures.py` 的 `extract_douban_topic_fields()` 从话题页 `rendered.html` 用正则提取结构化字段：标题（`<h1>`）、作者五元组（`<span class="from">` 里的 `author_profile_url`/`author_platform_id`/`author_display_name` + `<img class="pil">` 的 `author_avatar_url` + 小组链接里的 `group_name` 写入 `author_description`）、发帖时间（`<span class="create-time">`，解析为 Asia/Shanghai）、正文（`<div id="link-report">`，清洗 script/style/标签后写 `content_text`）。这些字段让豆瓣从「只有证据 URL」升级为结构化入库。

**搜索页过滤与城市映射**：豆瓣小组搜索页 `/group/search?q=...` 是发现话题 URL 的中间产物，不是单条内容。`web_post_for_capture()` 对豆瓣用 `is_douban_topic_url()` 校验，只让 `/group/topic/{id}/` 详情页进 `web_posts`，搜索页 `return None`（capture 仍留 `ctf_captures` 证据层）。城市映射依赖 keyword：`crawl_runner.py` 的 ctf_resource_crawl 分支会把 `crawl_targets.json` 里的 `keyword`（如「济南旅游」）通过 `--keyword` 传给 `ctf_resource_crawl.py`，写入 `capture_meta`；`import_ctf_captures.py` 读 `raw_meta.keyword`，经 `city_name_from_keyword()`（`SHANDONG_CITY_ALIASES` 山东 16 地市别名表）归一成标准市名写入 `city_name`。

**作者粉丝量补全**：话题页本身不含粉丝数（`rendered.html` 中「关注」命中 0 次），需单独访问作者主页 `https://www.douban.com/people/{id}/` 取数。people 页是服务端直出 HTML，`domcontentloaded` 即可解析，登录态（`dbcl2`）下可见完整信息。粉丝量取自 `<p class="rev-link"><a href=".../rev_contacts">...被{N}人关注</a></p>`，正则兼容 `万` 后缀。实现上走独立 enrichment 脚本（仿 `ctf_login_warmup.py` 范式，复用同一 profile），按 `author_platform_id` 去重、`site_request_guard` 节流、单个作者失败 try/except 跳过，`UPDATE web_posts SET author_followers_count=? WHERE platform_key='douban_group' AND author_platform_id=?`（一次更新该作者所有帖），并在 `metrics_json` 标 `followers_source="people_page"`。

**边界**：隐私用户（people 页显示「由于用户的设置，无法查看主页内容」）的 `rev-link`/`friend` 块整个消失，此时 `author_followers_count` 保持 NULL 并标 `followers_source="privacy_restricted"`，**不能把 NULL 当 0**。people 页 JS 模板里的「该账号处于异常状态」文案出现在所有页面，是关注失败提示文案而非用户状态，不能用作判断信号。`author_posts_count` 在 people 页无对应字段，保持 NULL 不造数。

## 保守运行策略

项目内的保守策略集中在三个地方：

| 文件 | 作用 |
|---|---|
| `web_sites.py` | 每个平台的预算、最小间隔、冷却、是否移动端、登录要求。 |
| `crawl_policy.py` | 站点级锁、每日预算、单会话上限、随机抖动、冷却阻断。 |
| `human_flow.py` | 页面停留、鼠标移动、滚动、评论区概率访问和 CDP 触摸滚动。 |

对于 MediaCrawler 支持的平台，项目不再复刻复杂页面行为，而是降低项目侧调用频率、限制并发、强制关闭媒体下载，并把平台细节交给 MediaCrawler。

页面级抓取会按 `web_sites.py` 的 `preferred_engine` 选择 Playwright 或 Patchright。`max_scrolls` 会传入详情页停留流程，限制滚动次数。B站 Opus/图文页会从可见文本中的明确日期行提取 `published_at`。

调度配置默认使用 headed 浏览器以减少 headless 指纹；需要快速验证或无界面环境时，可以在 `crawl_runner.py` 上显式传 `--headless`。

## 目录解耦

路径定义集中在 `scripts/project_paths.py`：

- 项目根目录、`data/`、`outputs/`、`temp/`、`tools/`。
- 默认数据库和配置文件。
- MediaCrawler 输出、页面兜底输出、登录 warmup 输出。
- 调度运行目录、节流状态文件、锁目录。
- Playwright/Scrapling profile 和 state 目录。
- 常用 schema 文件路径。

脚本中需要创建目录时使用 `ensure_dir()` 或 `ensure_parent()`，避免每个脚本重复写 `Path(...).mkdir(...)` 和硬编码目录。

## 保留和删除原则

保留：

- 能长期运行的统一入口。
- 多入口共用的策略、失败分类、登录/导航辅助。
- 入库和校验脚本。
- MediaCrawler 对接脚本。

删除：

- 一次性探测脚本。
- 已被 MediaCrawler 替代的专用平台采集器。
- 历史对照测试脚本。
- 已合并到新文档中的阶段性报告。

如果后续需要新增能力，优先加到现有入口或配置里，不再新增零散脚本。
