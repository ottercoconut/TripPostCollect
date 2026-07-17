# 正式抓取执行契约

本文定义正式抓取的机器语义。数量、平台参数和必需字段 profile：通用平台在
`config/crawl_targets.json` 中配置；小红书在 `config/xhs_targets.json` 和
`config/xhs_pool.json` 中配置。本文不重复平台数值。

## 唯一入口

通用平台正式任务只通过 `scripts/crawl_runner.py` 执行；小红书正式任务只通过
`scripts/xhs_runner.py` 执行。其他入口的定位如下：

- `mediacrawler_crawl.py`：调度器调用的结构化执行器，也可用于 `--no-import` 诊断。
- `info_collection_benchmark.py`：通用平台性能和容量评估，不代表正式轮次完成，也不接受小红书。
- `ctf_resource_crawl.py`：固定 URL 页面证据执行器。当前正式配置没有此类任务，直接运行只用于
  开发或诊断验证；以后若配置 `job_kind=ctf_resource_crawl`，正式执行必须由调度器调用。

任何入口使用 `--no-import` 都是诊断运行。即使摘要或冻结状态因执行与产物校验通过而显示
`completed`，没有实际 SQLite 新增就不满足正式完成契约；不得用
`import_new_target_met=true`、退出码或状态文件替代 `import_result.inserted_rows` 校验。

## 行为与策略门禁

B站、微博、抖音和知乎的结构化任务必须按以下顺序执行：

1. `site_request_guard()` 检查平台级最小间隔、随机抖动、单会话预算、每日预算和冷却；
2. 使用正式抓取的持久 profile 完成登录态确认；
3. 在实际搜索使用的浏览器页面执行 `social_high_risk` 行为阶段，包括随机停留、鼠标移动
   和随机触摸/滚轮滚动；
4. 行为阶段通过后才执行原平台搜索、分页、字段补全和候选累计；
5. 汇总行为证据、分页证据和字段校验后决定是否入库。

请求预算的计量单位是一次正式平台抓取会话，不按页面加载产生的每个图片、脚本或底层 API
请求重复计数。该门禁不改变原平台分页大小、候选上限、字段映射或去重规则。

每个平台本轮摘要必须包含 `policy_events` 和 `behavior_evidence`。行为证据至少包含
`pause`、`mouse_moves`、`human_scroll_complete`、运行时指纹、可见页面阻断标记，以及截图
路径或明确的截图错误；证据 URL 解码后必须包含本轮真实关键词，不能用平台首页、旧关键词
搜索页或固定占位搜索页代替。
触摸滚动与滚轮滚动按 profile 随机选择，不要求每轮同时出现。只有
`behavior_validation.ok=true`、`formal_validation.behavior_evidence_ok=true` 和
`formal_validation.policy_evidence_ok=true` 才能进入入库；文件缺失、事件不完整、验证码、
频控或阻断都必须失败，不能用内容 JSONL 或退出码补签。

小红书在此门禁外再强制执行独立控制面：账号和浏览器 profile 一一绑定；storage state
静态保存为 AES-GCM 密文，只在运行目录短暂解密；操作人必须通过 `--account-id` 选择账号；
单账号租约只防止同一账号并发使用，不实施自动账号轮换、日预算、冷却或全池熔断。登录失效
进入 `login_required`，其他验证和频控信号只记录证据，后续重试、隔离、恢复和切号由操作人
决定。小红书只允许 `xhs_guarded`，不自动处理验证，不在失败中途切换账号。搜索结果、笔记
详情、作者主页和翻页必须产生分阶段随机等待证据，不能退回固定短等待。完整操作顺序只在
[`platforms/xhs.md`](platforms/xhs.md) 维护，本文只定义机器门禁。

小红书可由操作人显式请求一轮最多一次的 `comment-scroll`、`like-one` 或 `random` 帖子互动。
互动默认关闭，运行在独立详情页标签，结果写入 `behavior_evidence.post_interactions`；互动失败
默认不否定抓取和入库契约，只有页面明确出现验证码、频控、封禁或登录失效时终止当前轮。
点赞会产生真实平台副作用，只有显式参数才能启用。

通用平台的验证码、频控或拒绝访问会写入站点冷却，后续任务由同一策略门禁停止；小红书只
记录本轮证据，等待操作人指挥。断点续跑只校验本次新执行记录的行为与策略证据，不要求历史
摘要补造新字段。

## 数量定义

- `candidate_hard_limit`：本轮允许处理的实际原始候选总数。按实际返回记录计数，
  不是底层请求参数的同义词。
- `target_new_posts`：本轮必须取得并实际新增到 SQLite 的唯一有效图文数。
- `valid_new_count`：完成视频过滤、平台 ID 去重、必需字段校验和作者字段补全后，且
  SQLite 中不存在相同平台 ID（缺失时按规范 URL）的记录数。
- `valid_existing_count`：字段校验有效、但 SQLite 已有对应记录的数量；它们可以刷新，
  但不计入 `target_new_posts`。
- `processed_rows`：执行过入库 upsert 的行数，不表示新增数或有效数。
- `inserted_rows` / `updated_rows`：数据库新增和更新数量，必须分别报告。
  `updated_rows` 表示相同平台 ID（缺失时用规范 URL）已存在，本轮用最新字段覆盖该行并
  重建其图片关系；它不是额外新增记录，也不表示平台内容一定发生过编辑。

正式结构化任务只有 `valid_new_count >= target_new_posts` 才能进入入库阶段，并且实际
`inserted_rows >= target_new_posts` 才能标记 `import_new_target_met=true`。不能用退出码、
`processed_rows`、`updated_rows`、少量样本或历史数据库总量替代。

## 有效记录

结构化图文记录至少满足：

- 有平台原始 ID 或规范 URL，且本轮唯一；
- 不是视频记录；
- 有正文或标题；
- 有平台原始发布时间；
- 有作者平台 ID 和作者昵称；
- 有至少一个正文图片 URL；
- 满足任务配置指定的作者粉丝量策略和平台字段 profile。

B站、微博、小红书、抖音、知乎使用 `followers_policy=required`。粉丝量为 `0` 只有在
平台响应明确出现该值且保存了 `followers_observed=true` 时有效；缺失值不得转换为
`0`。不提供粉丝量的平台使用 `followers_policy=ignored`，必须由配置声明，不能由
Agent 临场判断。

粉丝来源同时受平台 profile 限制：B站只接受 `relation_stat`，微博和知乎接受
`search_author`，小红书和抖音只接受 `creator_profile`。抖音搜索作者对象中的占位 0
不能替代作者主页结果。

小红书必须为每条候选图文取得当前作者主页证据：先使用登录会话的无 token 作者页请求，
空结果才允许在随机等待后通过同一已登录 BrowserContext 打开无 token 作者页。笔记
`xsec_token` 不能作为作者主页凭据。作者页仍为空时该候选无效；页面出现验证、频控或封禁
标记时本轮运行失败，不能把阻断降级为普通缺字段。

## 冻结执行状态

通用任务必须在 `data/runtime/crawl_execution_states/<run_id>/<job_key>.json` 生成状态；
小红书必须在 `data/runtime/xhs/execution_states/<run_id>/<target_key>.json` 生成状态。
状态文件冻结以下输入及 SHA-256：

- 当前配置文件；
- 本执行契约；
- 任务参数和实际命令；
- 断点续跑时使用的上一轮 `summary.json` 及其全部内容 JSONL。

阶段固定为：

1. `plan_frozen`
2. `command_executed`
3. `artifacts_verified`
4. `persistence_verified`
5. `task_finalized`

dry-run 只执行计划冻结，因此预期只有 `plan_frozen=completed`，后四阶段保持 `frozen`，
运行摘要任务状态为 `planned` 且入库明确跳过。不得把该预期状态误报为阶段阻断；真实运行
才要求五阶段全部 `completed`。

进入下一阶段前，程序必须重新读取状态文件，确认上一阶段为 `completed` 或明确
`skipped`，并重新校验冻结输入。任一阶段失败或冻结输入变化，后续阶段保持
`frozen`。Agent 只读取状态和摘要汇报，不得手工跳过、改写或补签阶段。

## 停止状态

- `target_new_met`：有效新增图文达到目标。
- `candidate_hard_limit_reached`：实际候选达到硬上限但目标未达成。
- `source_exhausted`：平台明确返回空页、空游标或 `has_more=false`，且状态文件存在对应
  `adaptive_search_stopped` 证据。
- `stagnated`：微博、抖音、知乎和小红书按连续配置批次没有新增满足正式字段 profile 且
  数据库中不存在的唯一记录累计。新的无效候选、重复候选和数据库已有记录都不能重置这些
  平台的停滞计数；批次事件仍分别记录新候选 ID 数和有效新增数，供定位分页进展与字段失败。
  B站当前自有 article 实现是明确例外：它按页面是否出现成功归一化且本轮未见的 article ID
  累计停滞；这类新 ID 即使后续正式字段校验无效或数据库已有，也会重置停滞计数。该例外不
  放宽完成标准，仍只有有效新增达到目标才算完成；判断 B站是否值得扩容时必须同时读取新 ID
  数和有效新增数。
- `login_required` / `captcha_detected`：登录或验证阻断。
- `runtime_failed`：浏览器或本地运行环境失败。

除 `target_new_met` 外，其余状态都不能汇报为正式结构化轮次完成。固定 URL 页面任务的成功
仍只代表该页面证据完成，不代表平台批量目标完成。

每个分页批次必须记录平台页码、请求游标或 search ID（平台提供时）、下一游标、原始
返回条数和 `has_more`（平台提供时）。仅有一条或多条 `adaptive_batch_completed`、但没有
`adaptive_search_stopped` 的任务，不得推断为 `source_exhausted`；目标未达成时统一按
`runtime_failed` 处理。分页循环以实际候选累计到 `candidate_hard_limit` 为边界，不得用
“页数 × 名义页大小”提前截断。

## 断点续跑

当前 `--resume-summary` 只支持微博、抖音和知乎这三个由底层 MediaCrawler 分页执行的
`mediacrawler_search` 任务。它们在接近目标时因字段差异、运行异常或单关键词耗尽而未入库，
可以通过调度器继续：续跑必须冻结上一轮摘要和 JSONL，将上一轮通过正式校验的新增及已有记录
ID 注入底层去重集合，并只抓剩余新增目标和候选预算；可用 `--start-page` 继续同一关键词后续页，
或用 `--recovery-keyword` 切换到能归一为同一城市的补充关键词。最终校验必须同时读取旧、新两轮
产物，达到完整 `target_new_posts` 后一次性入库；不得把未达标的部分产物单独导入。

B站 article 虽由通用调度器调用 `mediacrawler_crawl.py`，但使用项目自有 article 搜索实现，
不支持冻结断点续跑。B站新完整轮次的 dry-run 和正式命令均不得传入 `--resume-summary`、
`--recovery-keyword` 或 `--start-page`，由执行器默认从第 1 页开始。未达标时必须结束当前轮；
只有停止证据为 `candidate_hard_limit_reached` 且没有来源耗尽证据时，才调整下一轮配置中的候选
预算。新轮从零累计候选、新增和已有记录，不拼接或单独导入旧轮部分产物。调度器当前不会在
dry-run 阶段提前拒绝 B站的 `--resume-summary`，所以带恢复参数的 `planned` 状态不代表能力
验证通过，真实执行仍会拒绝。

恢复 dry-run 只能用于微博、抖音和知乎。冻结状态 `plan.command` 中，请求的
`--resume-summary` 和 `--start-page` 必须原样存在；`--recovery-keyword` 的值必须作为 child
命令的 `--keyword` 值存在。任一项不符时不得执行正式续跑。小红书独立 runner 当前也不接受
`--resume-summary`；失败后结束该轮，并严格按小红书失败分流决定同账号登录复验、调整下一轮
候选预算或关键词，或由操作人手工选择下一轮账号。任何换号都不得解释为同一正式轮次续跑。
