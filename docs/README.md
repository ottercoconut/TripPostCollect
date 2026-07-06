# TripPostCollect 总览

这份文档给非开发读者看，说明项目现在做什么、怎么运行、哪些地方由工具负责。更细的技术细节放在另外两份文档：

| 文档 | 用途 |
|---|---|
| [抓取架构](crawl-architecture.md) | 平台分工、调度器、MediaCrawler 对接、保守抓取策略。 |
| [入库与校验](data-persistence.md) | SQLite 表、入库脚本、查询和校验方法。 |
| [平台字段覆盖表](platform-field-coverage.md) | 各平台当前能结构化提供、只能保留证据或不能提供的字段。 |
| [反自动化检测与人类行为模拟](anti-automation-behavior.md) | 授权靶场中的随机间隔、行为模拟、浏览器指纹和稳定规避方案。 |

## 项目目标

项目用于在本次 CTF 授权靶场中持续抓取“济南旅游”相关图文内容，尽可能保留发帖时间、作者信息、图片资源、页面证据和运行摘要。日常运行要尽量稳定、低频、可恢复，并减少对 Agent 临场判断的依赖。

## 当前决策

能交给 MediaCrawler 做的平台，不再继续完善项目内的专用采集器。

| 平台 | 当前做法 | 原因 |
|---|---|---|
| B站 | 默认做 Opus/图文页面级抓取，并归一化到 `web_posts` | B站搜索结果容易返回视频；当前规则禁止视频采集。 |
| 微博 | MediaCrawler 搜索抓取，项目负责调度和入库 | 原自写脚本等待节奏固定，维护成本高。 |
| 小红书 | MediaCrawler 搜索抓取，项目负责作者主页补充和入库 | 网页端详情限制较严；使用可见浏览器/CDP、storage snapshot 登录态恢复和低频策略。 |
| 抖音 | MediaCrawler 搜索抓取，项目负责调度和入库 | 原自写脚本对页面状态和会话非常敏感。 |
| 知乎 | MediaCrawler 搜索抓取，项目负责调度和入库 | MediaCrawler 内容模型提供回答/文章发布时间；需要可用 Chrome/CDP 和已验证 cookie 快照。 |
| 携程、去哪儿、穷游、豆瓣小组 | 走通用页面/图片资源抓取，并归一化到 `web_posts` | MediaCrawler 不覆盖这些页面类型，项目同时保留低频证据抓取。 |

项目只采集图文内容、作者可见信息、图片 URL/图片样本和页面证据。视频目标、视频媒体请求和 MediaCrawler 返回的明确视频记录一律忽略或跳过，不作为抓取失败处理。

## 现在保留的脚本

`scripts/` 已收敛为少量长期有用的脚本：

| 脚本 | 职责 |
|---|---|
| `crawl_runner.py` | 统一调度入口，读取配置、选择到期任务、记录运行结果。 |
| `mediacrawler_crawl.py` | 调用 `tools/MediaCrawler`，抓取微博、小红书、抖音、知乎等非视频结构化结果，并导入 `web_posts`。 |
| `mediacrawler_login_warmup.py` | 需要人工登录时，打开 MediaCrawler 支持的平台登录窗口，并保存已验证登录态快照。 |
| `mediacrawler_batch_validate.py` | 按批次执行小红书/抖音抓取，每批立即校验字段；小红书会先做可见 preflight、截图和 storage snapshot 登录态恢复。 |
| `ctf_resource_crawl.py` | 对不适合 MediaCrawler 的站点做页面、图片、截图和 flag-like 文本兜底抓取。 |
| `import_ctf_captures.py` | 把 `ctf_resource_crawl.py` 的产物导入 `ctf_captures`，并把成功页面归一化写入 `web_posts`。 |
| `crawl_policy.py`、`human_flow.py`、`failure_classifier.py` | 共享节流、页面停留/滚动、失败分类。 |
| `ctf_browser_resilience.py`、`ctf_scrapling_preflight.py` | 抖音导航稳定性和静态预检辅助。 |
| `web_sites.py` | 平台默认 URL、预算、冷却、登录和风险配置。 |

原来的单次探测脚本、历史对照脚本和旧专用采集器已经删除，避免日常使用时选错入口。

## 日常怎么运行

`crawl_runner.py`、`mediacrawler_crawl.py` 入库流程和 `import_ctf_captures.py` 都会自动补齐数据库 schema、平台注册表和调度配置。需要只刷新/检查调度库时，可以显式运行：

```bash
.venv/bin/python scripts/crawl_runner.py --sync-only
```

查看将要执行的任务，不真正抓取：

```bash
.venv/bin/python scripts/crawl_runner.py --dry-run --max-jobs 5
```

执行到期任务：

```bash
.venv/bin/python scripts/crawl_runner.py --max-jobs 3
```

调度配置默认使用可见浏览器窗口，降低 headless 指纹。只做快速验证或无界面运行时再显式加 `--headless`。

刷新知乎等 MediaCrawler 平台登录态：

```bash
source .venv/bin/activate
python scripts/mediacrawler_login_warmup.py \
  --platforms zhihu \
  --timeout-seconds 600
```

知乎 warmup 成功后会重开验证登录态，并在 `tools/MediaCrawler/browser_data/zhihu_user_data_dir/trippostcollect_cookie_snapshot.json` 保存本地 cookie 快照；抓取脚本缺少有效 `d_c0/z_c0` 快照时会早停。小红书还会保存 `tools/MediaCrawler/browser_data/xhs_user_data_dir/trippostcollect_storage_state.json`，preflight 和正式抓取窗口都会用它恢复 cookies、localStorage 和 sessionStorage，避免只依赖 Chromium profile 的 `Default/Cookies`。

只刷新小红书 storage snapshot，不执行正式抓取：

```bash
source .venv/bin/activate
python scripts/mediacrawler_batch_validate.py \
  --xhs-preflight-only \
  --platforms xhs \
  --target-count 10 \
  --batch-size 10 \
  --login-type cookie \
  --xhs-preflight-timeout 300 \
  --xhs-initial-delay-seconds 8 \
  --xhs-screenshot-interval 5
```

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

只跑一个页面级兜底抓取：

```bash
.venv/bin/python scripts/ctf_resource_crawl.py \
  --sites bilibili \
  --headless \
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

旧结构产物不再保留 `legacy/` 归档。被淘汰的一次性探测、历史对照测试和旧专用采集产物可以直接删除；长期数据以 SQLite 为准。

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
