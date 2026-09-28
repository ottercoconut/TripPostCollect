# TripPostCollect 文档入口

TripPostCollect 用于授权 CTF 靶场中的低频图文抓取、证据保留和 SQLite 入库。现行行为以代码、
配置、测试和本页指向的权威文档为准；已完成的阶段计划与验收报告从 Git 历史查询，不在工作树中
持续维护。

## 按任务阅读

| 任务 | 必读文档 |
|---|---|
| 测试分组、临时副本、macOS 隔离与安装验收 | [可复用测试运行](testing.md) |
| 正式抓取、来源耗尽、成功和失败语义 | [正式抓取执行契约](formal-crawl-contract.md) |
| 命令、登录、恢复和结果检查 | [正式抓取运行手册](operations-runbook.md) |
| 抖音、微博、知乎历史详情修复 | [正式抓取运行手册](operations-runbook.md#通用平台历史详情修复) |
| 调度器、执行器和数据流 | [抓取架构](crawl-architecture.md) |
| MediaCrawler 上游同步和本地改造 | [MediaCrawler fork 维护](mediacrawler-fork-maintenance.md) |
| 本地分支清理、提交、配套版本和 Git 备份恢复 | [本地分支与版本管理](version-control.md) |
| SQLite、媒体文件、事务和入库校验 | [数据持久化](data-persistence.md) |
| 面向研究使用的帖子、作者、互动与图片字段 | [抓取结果数据字典](crawl-result-data-dictionary.md) |
| 五个平台的字段能力 | [平台字段覆盖](platform-field-coverage.md) |
| 小红书账号、登录、抓取和恢复 | [小红书 Workflow](platforms/xhs.md) |
| B站 article 详情与正文完整性 | [B站 article](platforms/bilibili.md) |
| 抖音搜索游标与图文详情 | [抖音](platforms/douyin.md) |
| 微博长文与图片字段 | [微博](platforms/weibo.md) |
| 知乎回答、文章与图片字段 | [知乎](platforms/zhihu.md) |
| 固定 URL 页面证据 | [页面证据平台](platforms/page-evidence.md) |
| 独立只读管理端 | [TripPostAdmin](../../TripPostAdmin/README.md) |

## 内容归属

平台迁移 v0.7：[主设计与历史研究](platform-adapters.md)、[详细迁移规格](platform-adapter-specification.md)、
[附录 C8 全量符号账](platform-adapter-symbol-ledger.md)、[P00 工作单与证据索引](platform-adapter-preflight.md)。
详细规格定义迁移清单、接口/读取时点、任务和门禁；C8 逐项处置每个定义；任务卡的执行清单以 GitHub issues 跟踪。
P00 已完成，四者均不构成实施授权、迁移完成报告或现行操作契约。

为避免同一规则在多篇文档中漂移，文档只维护各自负责的内容：

- 正式契约定义完成谓词、状态、错误分类、候选记忆和正式门禁。
- 运行手册只给出可执行命令、人工步骤、恢复动作和检查方法。
- 架构文档只描述组件、状态流和数据流。
- 持久化文档只描述 schema、事务、文件归属、导入与验证。
- 结果数据字典只描述用户查询字段和表间关系，不冻结会随抓取变化的数据库数量。
- 平台文档只描述该平台独有的登录、API、字段、游标和失败信号；共享规则直接引用正式契约。

新行为不得只写进 README、历史提交说明或 Skill。共享语义变化优先修改正式契约、代码和测试，
只有操作步骤、数据结构或平台差异确实变化时才同步相应文档。

## 正式入口

正式结构化抓取只保留全量来源耗尽流程。runner 持续处理未知候选，只有取得可验证的
`source_exhausted` 停止证据并完成字段、行为、正文图片和 SQLite 持久化门禁后才算完成。数量目标、
候选硬上限、停滞停止和完成模式选择均不再提供。完整完成判据见
[正式抓取执行契约](formal-crawl-contract.md)。

B站、微博、抖音和知乎从通用 runner 进入。runner 默认把本轮选中的不同平台放入最多 4 个并行通道，
同一平台 job 保持串行；小红书仍不参与该调度。先检查登录态：

```bash
source .venv/bin/activate
python scripts/login_warmup.py --targets all
```

查看到期任务计划：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --max-jobs 5
```

执行任务：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --max-jobs 3
```

小红书只从 `scripts/xhs_runner.py` 进入，使用独立逻辑账号槽位、租约、配置和执行状态。每个正式
轮次创建空临时 profile 并在轮内人工扫码，不读取或保存跨轮登录态。运行前必须完整执行
[小红书 Workflow](platforms/xhs.md)，不得放入通用 runner、warmup、独立登录命令或 benchmark。

## 不可跨越的边界

- 项目主题固定为青岛；由操作人在配置和执行前确认，不在抓取器中增加关键词硬门禁。
- 五个平台只采集图文、研究所需作者结构化字段、权威正文图片和证据；作者头像不采集、不下载、
  不持久化、不展示或统计，视频目标与视频媒体跳过。
- `web_posts` 是用户内容主表，`ctf_captures` 是证据与调试底座。
- `published_at` 只能来自平台原始发布时间，按 Asia/Shanghai ISO 保存。
- 正式正文图片必须经过 manifest、字节验证和 SQLite 事务入库；URL-only 不算完成。
- `--no-import`、benchmark 和直接调用 child 都属于诊断，不构成正式完成证据。

## 历史数据事件

2026-08-02 发现 B站 article 曾把搜索摘要当完整正文。历史修复、未成功项清理及最终数量只在
[B站正文完整性事件](incidents/2026-08-02-bilibili-article-completeness.md)记录，现行入口不重复维护
该历史库存。

已完成的图片本地化阶段计划、验收报告和 B站修复执行计划不再作为现行文档维护；需要追溯时使用
对应 Git 提交及历史文件。
