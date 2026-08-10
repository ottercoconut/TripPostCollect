# 文档一致性审计（2026-08-10）

## 范围与口径

本次审计覆盖变更前仓库内全部 31 份一方 Markdown、根目录 `AGENTS.md`，以及三个项目 Skills 的
`SKILL.md`、共享 `references/runbook.md` 和三个 `agents/openai.yaml`。第三方
`tools/MediaCrawler/` 文档不作为项目治理文档改写；涉及现行命令或平台行为时只以根项目封装和
精确实现核对。

文档分层如下：

- 现行入口与治理：`docs/README.md`、`AGENTS.md`、`docs/formal-crawl-contract.md`、
  `docs/operations-runbook.md`、`docs/crawl-architecture.md`、`docs/data-persistence.md`、
  `docs/platform-field-coverage.md`、`docs/admin-client-development.md`。
- 现行平台分支：`docs/platforms/bilibili.md`、`douyin.md`、`page-evidence.md`、`weibo.md`、
  `xhs.md`、`zhihu.md`。
- 事件记录：`docs/incidents/2026-08-02-bilibili-article-completeness.md`。
- 已完成计划与历史验收快照：`docs/plans/2026-08-02-bilibili-full-library-repair.md`、
  `2026-08-07-multiplatform-local-image-storage.md`，以及
  `2026-08-07-image-main-program-i00-baseline.md` 至 `i14-report.md` 全部 15 份阶段文档。

## 质疑结果与处理

| 质疑点 | 证据 | 处理 |
|---|---|---|
| “只收集青岛”是否只是口头前提 | README 与入库文档原先明确说不做关键词校验，代码入口也接受任意城市 | 建立共享范围门禁：声明关键词必须以“青岛”或“崂山”开头；同步更新治理、平台、运行和 Skill 文档 |
| 页面证据能否绕开范围 | `ctf_resource_crawl.py --keyword` 原为可选，导入器接受缺失主题 | 参数改为必填；抓取前和导入前分别校验；固定 URL 与声明主题的真实性仍由操作人负责 |
| 登录/诊断默认页是否符合项目范围 | 平台 registry 残留济南搜索和非青岛固定内容 URL | 五个平台默认面统一为“青岛旅游”搜索页，并增加自动测试 |
| 不保存 `city_name` 是否与范围控制冲突 | 旧文档把“不建模城市”等同于“不校验项目范围” | 明确分层：数据库不建城市列、不猜正文地名；应用入口校验任务声明 |
| 历史报告中的测试数、库存和临时路径是否仍是现行事实 | I-00/I-12 等记录的是 2026-08-07 当时结果，部分 `temp/` 已按清理要求删除 | README 统一声明历史快照优先级；相关报告补充临时产物可清理说明，不把其路径当现行依赖 |
| AGENTS、正式契约与三个 Skills 的模式路由是否冲突 | 两种模式对 `target-new-posts` / `source-exhausted` 的选择和完成谓词一致 | 保持现有模式语义，只补同一青岛范围前置门禁 |

## 自动与定向核对

- 全部一方 Markdown 相对链接可解析，没有断链。
- 文档引用的一方脚本、配置、schema 和源码路径存在；模板路径与历史临时路径按其文档语义处理。
- 文档命令使用的根项目 CLI 参数均存在于对应 `--help`；第三方工具参数不作为根项目接口。
- 当前通用配置 4 个启用 job 和小红书 2 个 target 的关键词均以“青岛”或“崂山”开头。
- 审计时默认库共有 6,719 条 `web_posts`、8 个不同关键词，全部通过同一范围门禁；B站 3,006 条，
  均带 `detail_observed` 详情证据，与现行 README、平台文档和事件文档口径一致。
- 受限冻结文档修改后必须重新生成 `config/frozen_files.json` 中对应 SHA-256，并通过
  `scripts/verify_frozen_files.py`；三个项目 Skills 还需分别通过 `quick_validate.py`。

## 长期规则

新增或修改文档时，应先判断它是现行契约、操作说明还是历史快照。现行行为不得只写在报告或
Skill 中，必须同时有代码门禁和测试；历史数字、提交和临时产物不得被描述为持续不变量。
