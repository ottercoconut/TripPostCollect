# 正式抓取执行契约

本文定义正式抓取的机器语义。数量、平台参数和必需字段 profile：通用平台在
`config/crawl_targets.json` 中配置；小红书在 `config/xhs_targets.json` 和
`config/xhs_pool.json` 中配置。本文不重复平台数值。

> 临时平台范围（2026-07-20）：当前只允许显式执行小红书独立 workflow；通用配置中的
> B站、微博、抖音和知乎任务全部 `enabled=false`。冻结不删除其 checkpoint 或候选记忆，
> 也不改变本文的通用完成语义。

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

- `candidate_hard_limit`：单次 child 执行允许进入字段校验的未知原始候选上限，不是跨多次
  续跑活动的总预算，也不是底层请求参数的同义词。数据库、当前累计摘要、对应平台的持久候选
  记忆表或本次已见集合中已知的平台 ID 会在详情、作者补全和媒体处理前跳过，不消耗该预算。
- `target_new_posts`：本轮必须取得并实际新增到 SQLite 的唯一有效图文数。
- `valid_new_count`：完成视频过滤、平台 ID 去重、必需字段校验和作者字段补全后，且
  SQLite 中不存在相同平台 ID（缺失时按规范 URL）的记录数。
- `valid_existing_count`：本次产物中完成字段校验、但 SQLite 已有对应记录的数量；它们不计入
  `target_new_posts`。发现阶段提前识别并跳过的已知 ID 不进入本计数。
- `processed_rows`：执行过入库 upsert 的行数，不表示新增数或有效数。
- `inserted_rows` / `updated_rows`：数据库新增和更新数量，必须分别报告。
  `updated_rows` 表示相同平台 ID（缺失时用规范 URL）已存在，本轮用最新字段覆盖该行并
  重建其图片关系；它不是额外新增记录，也不表示平台内容一定发生过编辑。

候选累计从 0 开始，按平台实际分页逐个增加；达到 `target_new_posts`、来源明确耗尽、连续停滞、
运行超时或触及 `candidate_hard_limit` 才停止。`candidate_hard_limit` 不是要求底层预取或处理的
固定数量。小红书提高正式目标时必须同时核对候选上限、`max_stagnant_batches`、
`top_refresh_max_pages`、`timeout_seconds` 和账号 `lease_seconds`；租约必须至少覆盖超时加 300 秒。
这些运行预算不属于查询来源参数，调整后继续使用原目标、账号和查询指纹对应的 checkpoint，
但必须通过新的 dry-run 冻结并核对实际计划。

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
- 任务参数；通用 runner 同时冻结实际 child 命令。小红书在 dry-run 时只冻结账号、目标、互动
  参数和发现计划，正式执行解密临时 storage state 后才构造 child 命令，并把命令写入
  `command_executed` 阶段证据；不得要求小红书 dry-run 预先包含不存在的临时路径或实际命令；
- 自动或人工跨次累计时使用的上一轮 `summary.json` 及其全部内容 JSONL。

阶段固定为：

1. `plan_frozen`
2. `command_executed`
3. `artifacts_verified`
4. `persistence_verified`
5. `task_finalized`

dry-run 只执行计划冻结，因此预期只有 `plan_frozen=completed`，后四阶段保持 `frozen`，
运行摘要任务状态为 `planned`。通用 runner 的每任务记录还写入
`import_result.skipped=true, reason=dry_run`；小红书 dry-run 没有 child summary 或
`import_result`，以未启动 child、后四阶段保持 `frozen` 证明未入库。不得把字段缺席误报为已
入库，也不得把该预期状态误报为阶段阻断；真实运行才要求五阶段全部 `completed`。

进入下一阶段前，程序必须重新读取状态文件，确认上一阶段为 `completed` 或明确
`skipped`，并重新校验冻结输入。任一阶段失败或冻结输入变化，后续阶段保持
`frozen`。Agent 只读取状态和摘要汇报，不得手工跳过、改写或补签阶段。

## 停止状态

- `target_new_met`：有效新增图文达到目标。
- `candidate_hard_limit_reached`：实际候选达到硬上限但目标未达成。
- `source_exhausted`：平台明确返回空页、空游标或 `has_more=false`，且状态文件存在对应
  `adaptive_search_stopped` 证据。抖音新鲜游标链的第 1 页是例外：`data=[]` 与
  `has_more=false` 不能单独证明耗尽；还必须由当前可见搜索页明确显示无结果，停止细节写为
  `verified_empty_first_page`。可见页仍有作品或页面状态不明确时必须写
  `runtime_failed`，分别使用 `empty_api_response_with_visible_results` 或
  `ambiguous_empty_first_page`，并保留第 1 页、offset 0、空 search ID 供下轮重取。
- `stagnated`：抖音、知乎和小红书按连续配置批次没有新增满足正式字段 profile 且数据库中
  不存在的唯一记录累计；新的无效候选、重复候选和数据库已有记录都不能重置停滞计数。微博
  按是否出现不在数据库、累计摘要、`crawl_discovery_seen_candidates` 和本 child 已见集合中的候选 ID 累计停滞：综合搜索连续出现纯文本或视频时仍推进扫描，
  只有连续批次没有新 ID 才停；正式完成仍只计算有效新增图文。批次事件记录
  `stagnation_basis`、新候选 ID 数和有效新增数，供区分“结果类型不合格”与“页面实际重复”。
  B站当前自有 article 实现是另一明确例外：它按页面是否出现成功归一化且不在数据库、累计摘要、
  `crawl_discovery_seen_candidates` 或本次已见集合中的 article ID 累计停滞；这类未知 ID 即使后续正式字段校验无效，也会重置
  停滞计数。该例外不
  放宽完成标准，仍只有有效新增达到目标才算完成；判断 B站是否值得扩容时必须同时读取新 ID
  数和有效新增数。
- `login_required` / `captcha_detected`：登录或验证阻断。
- `runtime_failed`：浏览器或本地运行环境失败。

除 `target_new_met` 外，其余状态都不能汇报为正式结构化轮次完成。固定 URL 页面任务的成功
仍只代表该页面证据完成，不代表平台批量目标完成。

每个分页批次必须记录平台页码、请求游标或 search ID（平台提供时）、下一游标、可恢复的
下一页/offset/cursor、批次是否完整、发现阶段、原始返回条数和 `has_more`（平台提供时）。
抖音搜索响应还必须验证 HTTP 可解析后的业务 envelope：非成功 `status_code`、缺失或非列表
`data`、缺失或非法 `has_more`，以及 `has_more=true` 但没有下一 `logid`，都属于
`runtime_failed`，不得降级成空页。状态文件保存脱敏的 `douyin_search_response_observed`；
新鲜第 1 页为空时另存 `douyin_empty_first_page_checked`，只记录计数、匹配到的可见无结果标记、
URL 和检查错误，不保存完整响应或整页文本。
仅有一条或多条 `adaptive_batch_completed`、但没有
`adaptive_search_stopped` 的任务，不得推断为 `source_exhausted`；目标未达成时统一按
`runtime_failed` 处理。分页循环以实际候选累计到 `candidate_hard_limit` 为边界，不得用
“页数 × 名义页大小”提前截断。

## 持久化发现记忆与跨次累计

B站、微博、抖音和知乎的正式 `mediacrawler_search` 任务由通用调度器自动维护发现记忆。SQLite
`crawl_discovery_checkpoints` 与 `crawl_discovery_seen_candidates` 均以
`job_id + query_fingerprint` 隔离；前者保存安全前沿，后者保存已完成处理的候选 ID。小红书由独立 runner
维护 `xhs_discovery_checkpoints`，以 `target_key + account_id + query_fingerprint` 唯一定位，
并在同一作用域的 `xhs_discovery_seen_candidates` 保存已完成处理的候选 ID；两者不与通用任务或
其他账号共享未入库活动。指纹包含平台、关键词和影响来源结果的查询参数，
不包含候选上限、目标数、超时、登录和顶部刷新页数。关键词或来源查询参数改变时必须形成新
记忆，不得误用旧游标。

通用平台在昂贵处理前跳过 `web_posts` 已有 ID、累计摘要中的有效 ID、
`crawl_discovery_seen_candidates` 已处理 ID 和当前 child 已见 ID；小红书读取独立的
`xhs_discovery_seen_candidates`。五个平台的视频、字段无效和有效候选都在 child 摘要形成后获得
跨轮记忆，进程在摘要前崩溃的候选不会被提前标记。两套表的作用域不同：通用平台按 job 与查询
指纹隔离，小红书还按人工指定账号隔离，不能跨账号共享未入库活动。

首次执行从来源第一页开始，不做顶部刷新。每个完整前沿批次把下一页写入状态事件；抖音还必须
同时写入下一 offset 和响应 search ID，小红书保存本次搜索使用的 client search ID。对应根执行器
在 child 摘要形成后才把状态事件提交到 SQLite，不得由底层循环提前推进数据库游标。中途停止的
批次保存当前请求位置，下一次允许重取该批次，依靠已知 ID 提前去重；这样可以重复少量边界
数据，但不能跳过未持久化候选。小红书只有在 child 摘要形成后才把本轮候选 ID 与前沿一起提交；
视频、字段无效和有效候选都会获得发现记忆，进程在摘要前崩溃的候选不会被提前标记为已处理。

存在 checkpoint 时，runner 自动把保存位置传给 child；有未完成累计摘要时再传入上一份
`summary.json`。child 先刷新
配置的 `top_refresh_max_pages` 个顶部页面，再从保存前沿继续。顶部刷新用于发现新近发布内容，
不推进深层 checkpoint，也不累计前沿停滞；已知 ID 在详情、粉丝和媒体处理前跳过。深层批次
完整结束才推进到下一页；抖音恢复命令必须同时包含 `--start-page`、`--start-offset` 和非空
`--start-cursor`，小红书深页恢复必须同时包含 `--start-page` 和非空 `--start-cursor`。只有
页码没有 search ID 的请求是无效恢复。

未达到目标的产物不单独入库。runner 保存其摘要路径，下一次将历史与本次 JSONL 合并校验，
并只向底层下发剩余新增目标；`candidate_hard_limit` 每次 child 执行重新提供完整预算，不从历史
候选数中扣减。累计 `valid_new_count` 达到完整 `target_new_posts` 后才一次性入库并清空累计摘要，
checkpoint 本身保留，供下一次定时任务继续向后发现。前沿推进但本次尚未达标时不增加连续失败，
下一次按任务正常调度间隔运行；无推进的运行错误仍按重试策略处理。

`source_exhausted` checkpoint 默认不再盲目请求原深页，只执行顶部刷新。抖音的耗尽只证明旧
search ID 游标链结束：若顶部刷新同时观察到至少一个不在数据库、累计摘要或持久候选集合中的
新候选 ID、最新响应 `has_more=true`
且带非空下一 search ID，child 必须记录 `discovery_frontier_reseeded`，从该刷新链的下一组三元组
建立新前沿并替换旧耗尽 checkpoint；任一条件缺失时保持耗尽，只做顶部刷新，避免每轮盲扫旧结果。
若顶部刷新已经达到 `target_new_met`，本轮立即结束且不建立新前沿，旧耗尽 checkpoint 保持不变；
下一次正式轮次再按相同双重证据判断是否需要建立新 cursor 前沿。
来源重新出现未知内容时仍可累计。`--no-import` 诊断自动禁用 checkpoint 写入。通用 runner 的显式 `--resume-summary`、
`--start-page` 和 `--recovery-keyword` 仅用于人工恢复，不是正常 workflow；使用显式参数时调度器
不自动混入旧 checkpoint。小红书不开放这些人工恢复参数，全部由独立 runner 从账号级 checkpoint
生成；换号产生独立记忆，不得解释为同一正式轮次续跑。旧 `search_id` 恢复失败时必须保留原
checkpoint 并按 `runtime_failed` 停止，不得静默生成新 ID 请求猜测的深页。
