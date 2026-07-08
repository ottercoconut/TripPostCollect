---
name: mediacrawler-structured-crawl
description: 仅用于运行、调试或复核 TripPostCollect 面向 bilibili、xhs、weibo、douyin、zhihu 的结构化抓取，包括 B站 article 手工图文搜索、低频可见浏览器运行和登录态/浏览器配置目录稳定性。
---

# MediaCrawler 结构化抓取

## 触发条件

- 用户要求抓取或验证 bilibili、xhs、weibo、douyin、zhihu 的结构化结果。
- 改动涉及 `scripts/mediacrawler_crawl.py`、`scripts/mediacrawler_login_warmup.py`，或 `config/crawl_targets.json` 中的 `mediacrawler_search` 任务。
- 需要复核 MediaCrawler/B站 article 搜索运行摘要、JSONL 导入问题、登录态快照问题或平台字段覆盖问题。

## 输入材料

- `docs/crawl-architecture.md`
- 涉及可见/无界面浏览器、浏览器配置目录、低频、冷却或反检测决策时读 `docs/anti-automation-behavior.md`
- `docs/data-persistence.md`
- `docs/platform-field-coverage.md`
- `config/crawl_targets.json`
- `scripts/mediacrawler_crawl.py`
- 登录态/浏览器配置目录问题读 `scripts/mediacrawler_login_warmup.py`

## 工作流程

1. 确认平台是否配置为 `mediacrawler_search`；如已调度，检查 `config/crawl_targets.json` 中的任务。
2. 遇到知乎登录失败时，先按文档中的预热命令刷新登录态，再重新抓取；确认摘要报告了有效 cookie 快照。
3. 知乎抓取默认通过本地 `d_c0/z_c0` cookie 快照注入登录态；正式抓取启动前应清理 Chromium Session/Last Tabs 历史标签页恢复文件，启动后只保留本次运行页并等待登录态稳定。正文 HTML 图片应在清洗为纯文本前保留为 `image_list/image_count`，再由统一导入写入 `web_post_images`。
4. 小红书登录失败或 snapshot 缺失时，先运行 `scripts/mediacrawler_login_warmup.py --platforms xhs`；确认左侧“我”入口通过重开验证，并写出 `tools/MediaCrawler/browser_data/xhs_user_data_dir/trippostcollect_storage_state.json` 后再抓取。`mediacrawler_batch_validate.py --xhs-preflight-only` 只用于恢复检查和截图证据，不作为主登录刷新入口。
5. 对 bilibili、xhs、weibo、douyin、zhihu，修改频率、登录态/浏览器配置目录处理、无界面模式、图片下载或重试行为前，先检查 `docs/anti-automation-behavior.md`。
6. MediaCrawler CDP 浏览器应优先使用本机 Playwright Chrome for Testing，与页面级 Patchright 兜底共享同一套 Chrome；不要恢复旧的双 Chromium 路径。
7. 运行能回答请求的最小安全抓取。只有明确做纯取证检查或临时数据库导入验证时才使用 `--no-import`。
8. 任何抓取任务成功后，除非用户明确要求只抓取/不入库，否则把已接受记录同步到目标 SQLite 数据库。批量校验产物或其他只写 JSONL 的封装流程，应先用现有 MediaCrawler 导入路径把验证通过的记录导入 `web_posts` 和 `web_post_images`，再报告完成。
9. 检查本次运行的 `summary.json` 和 `summary.md`：状态、`non_video_content_records`、`published_at_records`、跳过视频数、图片数、解析错误、样本，以及存在时的 `import_result`。
10. 查询目标 SQLite 数据库的行数、关键标识、城市/关键词、时间戳、作者字段和图片行。成功抓取后若数据库缺少记录，视为工作未完成，而不是已完成抓取。
11. 字段从不可用、仅证据、条件支持、结构化支持之间变化时，更新 `docs/platform-field-coverage.md`。

## 验证

- 先运行 `source .venv/bin/activate`，之后只用 `python`。
- Python 改动：`python -m py_compile scripts/mediacrawler_crawl.py scripts/mediacrawler_login_warmup.py`
- JSON 配置改动：`python -m json.tool config/crawl_targets.json >/dev/null`
- 成功运行证据：`outputs/mediacrawler_runs/` 下有 `summary.json`，其中 `failed_count` 已分类，抓到内容时包含样本。
- 导入证据：针对目标平台查询 SQLite 中的 `web_posts` 和 `web_post_images`。
- 完成证据：非纯抓取任务的数据库行数必须与已接受/已验证记录一致；如有差异必须解释。
- 使用 `temp/` 做临时验证时，提取结论后删除本次创建的 `temp/<task>/`。

## 常见错误

- 直接读取完整 JSONL 或日志，而不是使用摘要计数、样本和定向查询。
- 在命令行传递原始 cookie；封装脚本使用环境变量和本地快照。
- 把知乎缺少 `d_c0/z_c0` 当作抓取器失败，而不是先刷新登录态。
- 把小红书缺少 `trippostcollect_storage_state.json` 当作抓取器失败，而不是先运行 `mediacrawler_login_warmup.py --platforms xhs`。
- 清理知乎历史标签页时删除整个 profile，导致 cookie、localStorage 或 cookie snapshot 丢失。
- 修知乎图片时只看 `content_text`，忽略搜索 API 原始 `content` HTML 中的 `<img>`。
- 打开宽泛的媒体下载参数，而不是使用项目封装行为。
- 没有运行产物或数据库查询就更新字段覆盖表。
- 登录、验证码或验证失败后不经过冷却和预热，快速重试高风险平台。
- 看到 JSONL 或 `valid_count` 证据就停止，未把已接受抓取记录同步到 SQLite。

## 参考文件

- `docs/crawl-architecture.md`
- `docs/anti-automation-behavior.md`
- `docs/data-persistence.md`
- `docs/platform-field-coverage.md`
- `scripts/mediacrawler_crawl.py`
- `scripts/mediacrawler_login_warmup.py`

## 脚本

- 现有：`scripts/mediacrawler_crawl.py`
- 现有：`scripts/mediacrawler_login_warmup.py`
