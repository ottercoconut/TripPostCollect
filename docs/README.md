# TripPostCollect 文档入口

TripPostCollect 用于授权 CTF 靶场中的低频图文抓取、证据保留和 SQLite 入库。
正式运行尽量由脚本和状态机完成，不依赖 Agent 临场解释数量、成功或重试语义。

> B站、微博、抖音、知乎和小红书五个平台默认均可运行。通用 runner 只调度配置中现存且
> `enabled=true` 的 job；小红书因账号隔离使用显式独立 runner，配置 schema v2 不设 `enabled`
> 字段。这个入口差异不构成平台启停策略。

## 首先阅读

根据任务只读取对应文档：

| 任务 | 权威文档 |
|---|---|
| 正式抓取、数量和成功语义 | [正式抓取执行契约](formal-crawl-contract.md) |
| 运行、登录、Chrome 和故障处理 | [正式抓取运行手册](operations-runbook.md) |
| 调度器、执行器和数据流 | [抓取架构](crawl-architecture.md) |
| SQLite、字段映射和入库 | [数据持久化](data-persistence.md) |
| 当前平台字段能力 | [平台字段覆盖](platform-field-coverage.md) |
| 五平台正文图片本地存储实施 | [五平台正文图片本地存储工程实现方案](plans/2026-08-07-multiplatform-local-image-storage.md) |
| 历史图片输入冻结与工具演练 | [H-00 输入冻结](plans/2026-08-07-historical-image-h00-input-freeze.md)、[H-01 工具验收](plans/2026-08-07-historical-image-h01-tooling-report.md) |
| B站专栏正文、详情失败与历史回填 | [B站 article](platforms/bilibili.md) |
| 管理端开发 | [管理客户端开发](admin-client-development.md) |
| 小红书账号、登录、正式抓取和恢复 | [小红书正式抓取 Workflow](platforms/xhs.md) |

平台细节位于 `docs/platforms/`。

## 当前数据质量事件

- 2026-08-02 确认 B站正式 article 分支曾把搜索摘要误当完整正文；影响范围、证据、修复阶段和
  历史回填边界见 [B站 article 正文完整性事件](incidents/2026-08-02-bilibili-article-completeness.md)。
  新抓取详情逻辑已通过测试与真实小样。历史回填于 2026-08-07 完成：冻结范围 3,009 条中
  3,006 条已取得详情并原位更新，另有 2 条 `operator_excluded` 和 1 条 `invalid_detail`。用户随后
  明确要求从当前业务数据删除这 3 条未成功修复记录；当前默认库保留 3,006 条 B站记录，全部具有
  `content_detail_status=detail_observed`。全库逐条哈希/图片关系校验通过，5 条分层实时复取样本正文
  和图片集合全部精确一致，未发现截断。历史 sidecar、原始 artifact、报告和修复前备份仅作为审计/
  恢复证据保留，不属于当前内容数据。完整口径和证据见
  [B站全库记录修复计划](plans/2026-08-02-bilibili-full-library-repair.md)。

## 正式入口

正式抓取先选择完成模式。两个模式共用 `trippostcollect-crawl` 共享核心、平台实现、SQLite
checkpoint、累计摘要和候选记忆；模式切换不清空进度，也不修改长期配置：

| 用户意图 | 必须使用的模式 Skill | runner 参数 | 完成判据 |
|---|---|---|---|
| 新增 N 条、达到配置数量或普通正式抓取 | `trippostcollect-crawl-to-target` | `--completion-mode target-new-posts` | 实际新增并入库达到目标 |
| 明确要求当前关键词来源耗尽、不设数量限制或抓完结果 | `trippostcollect-crawl-to-source-exhaustion` | `--completion-mode source-exhausted` | 存在真实来源耗尽证据并完成入库 |

同一任务只能选择一个模式 Skill。用户没有明确要求来源耗尽时使用定量模式；目标很大、定量未达标
或普通“抓取”请求都不能推断为来源耗尽模式。

B站、微博、抖音和知乎的正式结构化抓取，在执行前统一验证并按需刷新登录态：

```bash
source .venv/bin/activate
python scripts/login_warmup.py --targets all
```

该脚本只处理持久登录态，不抓取内容、不导入数据库。已有登录态有效时直接通过；失效时
等待人工登录，并在关闭、重开同一 profile 后再次验证。`--targets all` 只展开为上述四个平台；
不包含小红书，也不验证页面证据执行器使用的独立浏览器 profile。

查看任务计划：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --completion-mode target-new-posts \
  --max-jobs 5
```

该命令不访问平台内容，但不是文件系统/SQLite 只读操作：默认会同步 `crawl_jobs`，并写本轮运行
摘要、run report 和 execution state。未指定 `--job-key` 时只计划已启用且到期的任务，因此同步
任务数可能大于本轮选中任务数；以 dry-run 摘要的 `jobs_selected` 和每任务状态为准。

执行到期任务：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --completion-mode target-new-posts \
  --max-jobs 3
```

通用平台正式数量、字段 profile、分页和停止条件只从 `config/crawl_targets.json` 读取；结构化平台
同时强制执行正式契约定义的行为与请求策略门禁。
当前长期配置包含已启用的 `mc_bilibili_qingdao_laoshan_guide_article`；它与其他通用 job 一样只能从
`crawl_runner.py` 进入。配置同步后，普通定量轮次会把它路由到项目自有 B站 article 详情分支，不会
回退到旧搜索摘要逻辑，也不会调用历史修复 supervisor。
CLI 不传 `--completion-mode` 时仍默认按配置的 `target_new_posts`、`candidate_hard_limit` 和停滞
边界限量执行。模式 Skill 为了让冻结计划可审计，会在 dry-run 和正式命令中显式传入
`target-new-posts` 或 `source-exhausted`。后者不写回长期配置，也不把后续轮次永久改为无限数量。
通用 runner 会为 B站、微博、抖音和知乎自动读取 SQLite 发现 checkpoint：先有限刷新顶部，
再从已保存前沿继续；正常运行不需要人工传 `--start-page` 或 `--resume-summary`。小红书仍走
独立 workflow，但 `xhs_runner.py` 会按目标、人工指定账号和查询指纹自动读取自己的 checkpoint，
冻结未完成累计摘要，先刷新顶部，再使用已保存的 `page + search_id` 继续深层发现。
每个任务会在 `data/runtime/crawl_execution_states/<run_id>/` 生成冻结状态文件；只有状态
文件和正式摘要同时满足执行契约，才能汇报完成。

五个平台正式 child 都由 runner 固定开启 `--download-images`，只下载平台详情/正文结构中显式
投影的正文图，并保持视频媒体关闭。平台会话先写本轮 staging 和 `image_manifest.jsonl`，根项目再
验证身份、路径、SHA、真实 MIME、尺寸和解码，正式模式才原子晋升到 `data/media`，并把 URL、
`local_path` 和字节证据与帖子放在同一 SQLite 事务中。头像、作者主页、封面、搜索预览、视频、
音乐和知乎公式图在下载前自动排除；作者头像仅可保留 `author_avatar` URL 参考，不下载也不参与
正文图计数。任何正文图失败都会阻止正式完成，不能降级为只存 URL。

正常默认模式下，小红书的实际候选量从 0 开始按页增长，只有通过详情前去重的未知候选才占预算；达到
`target_new_posts` 后立即停止，不会为了配置的 `candidate_hard_limit` 继续抓满。后者只是单次
child 的安全上限。永久提高目标时应在 `config/xhs_targets.json` 同步调整候选上限、停滞批次、
顶部刷新和超时，并在 `config/xhs_pool.json` 保证租约至少覆盖超时加 300 秒清理时间；这些运行
预算变化不改变查询指纹，也不清空既有账号级 checkpoint 或候选记忆。
小红书正式抓取的 BrowserContext 安装新标签页守卫后，任何新出现的标签页都不依赖创建来源或
验证码识别结果：平台自行弹出的页面，以及 crawler 为作者主页回退、互动或验证辅助而创建的页面，
都必须立即置前，并从出现时起至少保留 30 秒。滚动无位移、正常返回、异常退出和最终浏览器清理
均不得绕过该保护期；已识别的验证页继续按最长 600 秒的人工处理规则等待。独立
`xhs_login.py` 只负责单页人工登录与关闭/重开复验，不属于正式抓取 BrowserContext 的守卫范围。

## 抓取记忆速查

“所有平台都有抓取记忆”只指五个正式结构化搜索平台；固定 URL 页面证据任务没有分页发现
前沿，每个已配置 URL 仍是独立任务。五个平台的记忆作用域和能力并不完全相同：

| 平台 | 控制面记忆 | 保存的深层前沿 | 跨轮详情前去重 |
|---|---|---|---|
| B站、微博、知乎 | `crawl_discovery_checkpoints`，按 job 与查询指纹隔离 | 下一安全页 | SQLite 已入库 ID、累计摘要 ID、`crawl_discovery_seen_candidates` 中所有已完成处理候选 ID |
| 抖音 | `crawl_discovery_checkpoints`，按 job 与查询指纹隔离 | page、offset、响应 search ID 必须成组恢复 | SQLite 已入库 ID、累计摘要 ID、`crawl_discovery_seen_candidates` 中所有已完成处理候选 ID |
| 小红书 | `xhs_discovery_checkpoints`，按目标、人工指定账号与查询指纹隔离 | page 与 client search ID 必须成组恢复 | SQLite 已入库 ID、累计摘要 ID，以及 `xhs_discovery_seen_candidates` 中所有已完成处理候选 ID |

首次运行从第一页开始且顶部刷新页数为 0；存在 checkpoint 后才先刷新配置限定的顶部页，再从
保存的深层前沿继续。顶部刷新不推进深层前沿。目标或候选上限在一页中途触发时，checkpoint
保留当前请求位置，下一轮允许重取这个边界页；已进入上述去重集合的 ID 会在昂贵详情或作者补全
前跳过。

抖音的 `exhausted` 只结束已保存 search ID 的游标链：顶部刷新发现持久记忆中不存在的新候选 ID 且获得可继续的
新 search ID 时，从刷新链下一页建立新前沿；否则保持耗尽，不重复深扫旧结果。微博综合搜索的
连续停滞按“没有新微博 ID”计算，纯文本或视频页不会因暂时没有有效图文而过早截断。
抖音新鲜游标链的第 1 页若 API 返回空数据，必须再核对当前可见搜索页：页面仍有作品或没有
明确“无结果”提示时按运行异常保留第 1 页，不能建立耗尽 checkpoint；只有 API 业务状态正常、
`has_more=false` 且页面明确显示无结果，才记录 `verified_empty_first_page`。

五个平台都会在 child 摘要形成后持久记忆视频、有决定性证据的字段无效候选和有效候选；可恢复
请求失败不属于“已完成处理”。通用平台写
`crawl_discovery_seen_candidates`，按 job 与查询指纹隔离；小红书写独立表并额外按人工指定账号隔离。
正常运行一律让 runner 自动生成恢复参数；人工恢复仅按
[运行手册](operations-runbook.md) 的限制处理。

小红书不进入上述通用登录和调度链路。先完整读取
[小红书正式抓取 Workflow](platforms/xhs.md)，再使用独立账号目录、加密 storage state 和
人工指定账号执行。以下命令以新账号为例；已有账号跳过 `enroll`，先用
`xhs_accounts.py list` 核对状态：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py enroll \
  --account-id xhs-a01
python scripts/xhs_login.py \
  --account-id xhs-a01
python scripts/xhs_runner.py \
  --dry-run \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --completion-mode target-new-posts
```

dry-run 通过并经人工确认后，直接用相同账号、目标和互动参数去掉 `--dry-run`。XHS 配置
schema v2 已删除 pool/target 的 `enabled` 开关；正式运行只由显式 runner 命令触发，不再为每轮
修改配置或在结束后重新冻结。运行结束后检查顶层摘要、child summary、冻结状态和 SQLite。
dry-run 计划中的 `discovery` 必须与所选账号的
`xhs_discovery_checkpoints` 一致。小红书状态位于
`data/runtime/xhs/execution_states/<run_id>/`；其余平台仍位于通用状态目录。

## 数据边界

- `web_posts` 不保存 `city_name`；当前内容属于青岛是业务前提，不进入 schema、筛选或关键词校验。
- 只采集图文、作者公开可见信息、权威正文图片及页面证据；明确视频记录跳过。正式新抓图片长期
  保存在 `data/media`，`web_post_images` 同时保存来源 URL、本地相对路径、尺寸、MIME 和 SHA。
- 头像、作者主页资源、封面、搜索预览、视频、音乐及知乎公式图不属于正文图片，不下载到本地；
  作者头像可作为独立 URL 参考入库，但不算正文图。
- `web_posts` 是用户使用的统一内容主表；`ctf_captures` 是证据和调试底座。
- `published_at` 必须来自平台原始发布时间，保存为 Asia/Shanghai ISO。
- 结构化长期数据以 SQLite 为准，`outputs/` 是运行产物和摘要。
- 主程序本地图片能力与历史补全是两个阶段：只有验收报告写明 `MAIN_PROGRAM_READY=true` 后，才按
  工程方案 H 阶段补齐当前数据库已有记录；正常新抓不会隐式改写历史数据。

## 诊断与开发入口

任意 runner 或执行器使用 `--no-import`，以及运行 `info_collection_benchmark.py`，都只属于
诊断或开发验证。它们不能替代正式入库，也不能作为正式完成证据；小红书仍必须经独立账号
runner 执行，不能直接交给通用 MediaCrawler 入口。
