# TripPostCollect 总览

这份文档给非开发读者看，说明项目现在做什么、怎么运行、哪些地方由工具负责。更细的技术细节放在另外两份文档：

| 文档 | 用途 |
|---|---|
| [抓取架构](crawl-architecture.md) | 平台分工、调度器、MediaCrawler 对接、保守抓取策略。 |
| [入库与校验](data-persistence.md) | SQLite 表、入库脚本、查询和校验方法。 |
| [平台字段覆盖表](platform-field-coverage.md) | 各平台当前能结构化提供、只能保留证据或不能提供的字段。 |
| [反自动化检测与人类行为模拟](anti-automation-behavior.md) | 授权靶场中的随机间隔、行为模拟、浏览器指纹和稳定规避方案。 |
| [可视化管理客户端开发文档](admin-client-development.md) | FastAPI + React/Vite 管理台的产品范围、项目结构重构、记录中心 API、CRUD 边界和验收标准。 |

## 项目目标

项目用于在本次 CTF 授权靶场中持续抓取“济南旅游”相关图文内容，尽可能保留发帖时间、作者信息、图片资源、页面证据和运行摘要。日常运行要尽量稳定、低频、可恢复，并减少对 Agent 临场判断的依赖。

## 当前决策

能交给 MediaCrawler 做的平台，不再继续完善项目内的专用采集器。

| 平台 | 当前做法 | 原因 |
|---|---|---|
| B站 | 默认做 Opus/图文页面级抓取；手工关键词验证可走 `mediacrawler_crawl.py --platforms bilibili` 的专栏/图文 article 搜索分支 | B站视频搜索容易返回视频；当前规则禁止视频采集，article 搜索只入非视频图文。 |
| 微博 | MediaCrawler 搜索抓取，项目负责调度和入库 | 原自写脚本等待节奏固定；当前 store 会保留微博搜索结果中的图片 URL 和作者粉丝量。 |
| 小红书 | MediaCrawler 搜索抓取，项目负责作者主页补充和入库 | 网页端详情限制较严；使用可见浏览器/CDP、storage snapshot 登录态恢复和低频策略。 |
| 抖音 | MediaCrawler 搜索抓取，项目负责调度和入库 | 原自写脚本对页面状态和会话非常敏感。 |
| 知乎 | MediaCrawler 搜索抓取，项目负责调度和入库 | MediaCrawler 内容模型提供回答/文章发布时间和正文图片 URL；需要可用 Chrome/CDP 和已验证 cookie 快照。 |
| 携程、去哪儿、豆瓣小组 | 走通用页面/图片资源抓取，并归一化到 `web_posts` | MediaCrawler 不覆盖这些页面类型，项目同时保留低频证据抓取。 |

项目只采集图文内容、作者可见信息、图片 URL/图片样本和页面证据。视频目标、视频媒体请求和 MediaCrawler 返回的明确视频记录一律忽略或跳过，不作为抓取失败处理。

2026-07-06 的微博修复后，`tools/MediaCrawler/store/weibo/__init__.py` 会从 `mblog.user` 保留 `followers_count/fans_count`，并从 `mblog.pics` 保留 `image_list/image_count`。验证产物 `outputs/mediacrawler_runs/20260706T151935+0000/weibo/usable_50_validation.json` 显示：从 149 条候选中筛出 50 条唯一图文，正文、图片 URL、作者粉丝量、发布时间和互动指标均完整。

2026-07-08 的 B站图文验证后，`scripts/mediacrawler_crawl.py --platforms bilibili --keyword 烟台旅游 --mediacrawler-max-notes 50 --import-limit 50` 会走 B站专栏/图文 article 搜索，产物 `outputs/mediacrawler_runs/20260707T173332+0000/summary.json` 显示：50 条唯一图文全部入库，图片 URL、发布时间、作者昵称/ID、点赞/评论/浏览指标均可结构化保留。

2026-07-08 的知乎修复后，MediaCrawler 知乎搜索会在把正文 HTML 清洗为纯文本前提取正文图片 URL，保存为 `image_list/image_count`，并由统一导入写入 `web_post_images`。知乎抓取还会在启动 Chrome 前清理 profile 中的 Session/Last Tabs 会话恢复文件，并在启动后关闭旧标签页；这只清理历史页面，不清 cookie、localStorage 或 cookie snapshot。`outputs/mediacrawler_runs/20260707T193211+0000/summary.json` 显示：36 条非视频内容全部有发布时间，18 条 JSONL 记录带图片；导入前 20 条后，SQLite 中 8 条为有效图片记录。

## 现在保留的脚本

`scripts/` 已收敛为少量长期有用的脚本：

| 脚本 | 职责 |
|---|---|
| `crawl_runner.py` | 统一调度入口，读取配置、选择到期任务、记录运行结果。 |
| `mediacrawler_crawl.py` | 调用 `tools/MediaCrawler`，抓取微博、小红书、抖音、知乎等非视频结构化结果，并导入 `web_posts`。 |
| `mediacrawler_login_warmup.py` | 需要人工登录时，打开 MediaCrawler 支持的平台登录窗口，并保存已验证登录态快照。 |
| `mediacrawler_batch_validate.py` | 按批次执行小红书/抖音抓取，每批立即校验字段；小红书会先验证 storage snapshot 可恢复。 |
| `ctf_resource_crawl.py` | 对不适合 MediaCrawler 的站点做页面、图片、截图和 flag-like 文本兜底抓取。 |
| `import_ctf_captures.py` | 把 `ctf_resource_crawl.py` 的产物导入 `ctf_captures`，并把成功页面归一化写入 `web_posts`。 |
| `crawl_policy.py`、`human_flow.py`、`failure_classifier.py` | 共享节流、页面停留/滚动、失败分类。 |
| `ctf_browser_resilience.py`、`ctf_scrapling_preflight.py` | 抖音导航稳定性和静态预检辅助。 |
| `web_sites.py` | 平台默认 URL、预算、冷却、登录和风险配置。 |

原来的单次探测脚本、历史对照脚本和旧专用采集器已经删除，避免日常使用时选错入口。

## 日常怎么运行

`crawl_runner.py`、`mediacrawler_crawl.py` 入库流程和 `import_ctf_captures.py` 都会自动补齐数据库 schema、平台注册表和调度配置。需要只刷新/检查调度库时，可以显式运行：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py --sync-only
```

查看将要执行的任务，不真正抓取：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py --dry-run --max-jobs 5
```

执行到期任务：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py --max-jobs 3
```

调度配置默认使用可见浏览器窗口，降低 headless 指纹。只做快速验证或无界面运行时再显式加 `--headless`。

刷新知乎登录态：

```bash
source .venv/bin/activate
python scripts/mediacrawler_login_warmup.py \
  --platforms zhihu \
  --timeout-seconds 600
```

知乎 warmup 成功后会重开验证登录态，并在 `tools/MediaCrawler/browser_data/zhihu_user_data_dir/trippostcollect_cookie_snapshot.json` 保存本地 cookie 快照；抓取脚本缺少有效 `d_c0/z_c0` 快照时会早停。正式抓取会复用同一 profile，但启动前会清理 Chromium 的历史标签页恢复文件，避免窗口打开后堆满上次留下的知乎标签页。

刷新小红书登录态并保存 storage snapshot：

```bash
source .venv/bin/activate
python scripts/mediacrawler_login_warmup.py \
  --platforms xhs \
  --timeout-seconds 600
```

小红书 warmup 成功条件是页面侧出现左侧“我”入口；脚本会关闭并重开浏览器验证持久化，再写入 `tools/MediaCrawler/browser_data/xhs_user_data_dir/trippostcollect_storage_state.json`。preflight 和正式抓取窗口都会用它恢复 cookies、localStorage 和 sessionStorage，避免只依赖 Chromium profile 的 `Default/Cookies`。`mediacrawler_crawl.py --platforms xhs` 在默认 `cookie` 模式下缺少该 snapshot 会早停并提示先执行 warmup。

按 10 条一批验证小红书和抖音字段完整性：

```bash
source .venv/bin/activate
python scripts/mediacrawler_batch_validate.py \
  --keyword 济南旅游 \
  --platforms xhs douyin \
  --target-count 50 \
  --batch-size 10 \
  --login-type cookie \
  --timeout-per-batch 900 \
  --xhs-preflight-timeout 300 \
  --xhs-initial-delay-seconds 8 \
  --xhs-screenshot-interval 5
```

只跑一个 MediaCrawler 平台：

```bash
source .venv/bin/activate
python scripts/mediacrawler_crawl.py \
  --platforms xhs \
  --keyword 济南旅游 \
  --login-type qrcode \
  --headed \
  --download-images \
  --timeout-per-platform 420
```

微博图文字段验证时可以先只保存文件、不入库，再从 JSONL 中筛选图片和粉丝量完整的记录：

```bash
source .venv/bin/activate
python scripts/mediacrawler_crawl.py \
  --platforms weibo \
  --keyword 济南旅游 \
  --login-type cookie \
  --headed \
  --mediacrawler-max-notes 150 \
  --timeout-per-platform 1200 \
  --no-import
```

只跑一个页面级兜底抓取：

```bash
source .venv/bin/activate
python scripts/ctf_resource_crawl.py \
  --sites bilibili \
  --headless \
  --max-image-save 3 \
  --max-scrolls 2
```

页面级抓取的默认行为 profile 会保留较长详情页停留时间，适合低频正式运行，不适合烟测。只验证浏览器、登录态或页面基本可用性时，使用 `quick_probe` 并把输出写入 `temp/`，验证完及时删除对应目录：

```bash
source .venv/bin/activate
python scripts/ctf_resource_crawl.py \
  --sites bilibili ctrip qunar douban_group \
  --headless \
  --behavior-profile quick_probe \
  --max-image-save 1 \
  --max-scrolls 1 \
  --settle-min-ms 200 \
  --settle-max-ms 400 \
  --timeout 20000 \
  --commit-timeout 6000 \
  --readiness-timeout 8000 \
  --no-throttle \
  --output-dir temp/page_smoke
```

豆瓣小组显式 topic URL 需要复用已预热的配置站点 profile：

```bash
source .venv/bin/activate
python scripts/ctf_resource_crawl.py \
  --urls https://www.douban.com/group/topic/53104421/ \
  --site-label douban_group \
  --configured-site-urls \
  --keyword 济南旅游 \
  --max-image-save 3 \
  --max-scrolls 2
```

## 输出在哪里

| 目录 | 内容 |
|---|---|
| `outputs/mediacrawler_runs/` | MediaCrawler 当前运行的 JSONL staging、日志、摘要；成功入库后可按保留策略清理。 |
| `outputs/ctf_resource_crawls/` | 页面级兜底抓取的 staging、截图、图片样本和日志证据；用户使用的数据以 `web_posts` 为准。 |
| `data/runtime/crawl_runner/` | 调度器每次运行的 `run_summary.json` 和 Markdown 摘要。 |
| `data/trippostcollect.sqlite` | 默认 SQLite 数据库。 |
| `temp/` | 只用于临时验证和一次性测试，可随时清空；正式流程不得依赖这里的文件。 |

旧结构产物不再保留 `legacy/` 归档。被淘汰的一次性探测、历史对照测试和旧专用采集产物可以直接删除；长期数据以 SQLite 为准。临时验证完成后应删除本次创建的 `temp/<任务名>/` 目录，只把必要结论同步到文档、SQLite 或 `outputs/` 摘要。

项目路径统一由 `scripts/project_paths.py` 定义。新增脚本不要自行硬编码 `outputs/`、`temp/`、`data/runtime/` 或 profile 目录。

## Agent 还做什么

现在的目标不是完全去掉 Agent，而是把日常重复判断下沉到脚本。

| 事项 | 谁负责 |
|---|---|
| 启动一次运行、查看摘要、决定是否调整范围 | Agent 或用户 |
| 判断哪些任务到期、跑哪个平台、失败是否重试 | `crawl_runner.py` |
| 微博/小红书/抖音/知乎等非视频平台字段抓取 | MediaCrawler |
| B站 Opus/图文页、页面证据、图片资源、截图和兜底 flag-like 文本 | `ctf_resource_crawl.py` |
| 入库、去重、基础校验、证据到 `web_posts` 的归一化 | 脚本和 SQLite |

这样做以后，换不同模型启动流程，对抓取结果的影响会明显降低。
