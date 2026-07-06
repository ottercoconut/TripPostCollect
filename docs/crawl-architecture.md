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
| B站 | `ctf_resource_crawl` | `ctf_resource_crawl.py --sites bilibili` | `web_posts`、`web_post_images`、`ctf_captures`、`ctf_capture_images` |
| 微博 | `mediacrawler_search` | `mediacrawler_crawl.py --platforms weibo` | `web_posts`、`web_post_images` |
| 小红书 | `mediacrawler_search` | `mediacrawler_crawl.py --platforms xhs` | `web_posts`、`web_post_images` |
| 抖音 | `mediacrawler_search` | `mediacrawler_crawl.py --platforms douyin` | `web_posts`、`web_post_images` |
| 知乎 | `mediacrawler_search` | `mediacrawler_crawl.py --platforms zhihu` | `web_posts`、`web_post_images` |
| 携程 | `ctf_resource_crawl` | `ctf_resource_crawl.py --sites ctrip` | `web_posts`、`web_post_images`、`ctf_captures`、`ctf_capture_images` |
| 去哪儿 | `ctf_resource_crawl` | `ctf_resource_crawl.py --sites qunar` | `web_posts`、`web_post_images`、`ctf_captures`、`ctf_capture_images` |
| 穷游 | `ctf_resource_crawl` | `ctf_resource_crawl.py --sites qyer` | `web_posts`、`web_post_images`、`ctf_captures`、`ctf_capture_images` |
| 豆瓣小组 | `ctf_resource_crawl` | `ctf_resource_crawl.py --sites douban_group` | `web_posts`、`web_post_images`、`ctf_captures`、`ctf_capture_images` |

知乎默认走 MediaCrawler 搜索链路。该链路依赖可用 Chrome/CDP 和登录态；如果登录态失效，应先用 `mediacrawler_login_warmup.py --platforms zhihu` 刷新，再运行调度。登录 warmup 会使用与 MediaCrawler CDP 相同的浏览器可执行文件和 `tools/MediaCrawler/browser_data/zhihu_user_data_dir`，登录成功后默认关闭并重开一次验证持久化是否生效，并把已验证的 `d_c0/z_c0` cookie 保存到本地快照 `trippostcollect_cookie_snapshot.json`。`mediacrawler_crawl.py --platforms zhihu` 会优先读取该快照并通过环境变量注入 MediaCrawler；如果快照缺失或不含必需 cookie，则直接早停，不再启动知乎抓取浏览器。

小红书也依赖 MediaCrawler 搜索链路，但登录态处理不同。实测中，小红书扫码成功后运行中的 CDP context 能看到登录 UI 和 `web_session/a1/webId/gid`，关闭窗口后 Chromium profile 的 `Default/Cookies` 仍可能为空，导致下次新窗口再次扫码。因此项目侧把小红书登录态保存为 `tools/MediaCrawler/browser_data/xhs_user_data_dir/trippostcollect_storage_state.json`，其中包含 cookies、localStorage 和 sessionStorage。`mediacrawler_batch_validate.py` 的小红书 preflight 会在看到“我”入口后导出该 snapshot；MediaCrawler 小红书启动时会先恢复 snapshot，再创建 API client，并在 `pong=True` 后写回新的 snapshot。

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
   - 小红书优先开启可见浏览器/CDP，启动时恢复 `trippostcollect_storage_state.json`，并在搜索结果后补拉作者主页指标；搜索结果含视频时会继续翻页补足图文记录。
   - 知乎开启 CDP 模式，搜索结果中的回答/文章 `created_time` 会映射到 `published_at`；登录态从本地 cookie 快照注入，`zvideo` 记录按视频跳过。
   - `published_at` 入库时统一保存为 Asia/Shanghai ISO 字符串；运行审计类 `captured_at` 仍保留 UTC。
   - 配置中的 `download_images: true` 只会在小红书任务上翻译为项目侧 `--download-images`；视频保存分支仍被禁用。
3. 统计 JSONL 内容记录、评论记录、作者字段、图片文件和异常视频文件数量。
4. 默认把非视频 JSONL 内容导入 `web_posts` 和 `web_post_images`，并写入平台原始发帖时间 `published_at`；明确视频记录只计入 `skipped_video`，不入库。

B站默认不再走 MediaCrawler 搜索调度，因为搜索结果容易返回视频。保留 `bilibili` 参数只用于手工验证非视频记录；如果上游返回视频记录，导入阶段会跳过。

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
