# 正式抓取执行契约

> **受限冻结：** 本文件属于治理基线；普通抓取、排障或顺手同步不得修改，只有用户明确授权治理变更，并同步核验对应代码、测试与关联文档时才允许更新。

本文定义正式抓取的机器语义。数量、平台参数和必需字段 profile：通用平台在
`config/crawl_targets.json` 中配置；小红书在 `config/xhs_targets.json` 和
`config/xhs_pool.json` 中配置。本文不重复平台数值。

> B站、微博、抖音、知乎和小红书五个平台默认均可运行。通用 runner 只选择当前配置中
> `enabled=true` 的 job；小红书因账号隔离由操作人显式运行独立 runner。两类入口服从相同的
> 完成模式和正式证据门禁。

操作层使用 `trippostcollect-crawl` 作为共享核心，并为每个正式任务选择且只选择一个模式 Skill：

- `trippostcollect-crawl-to-target` 固定使用 `target-new-posts`；
- `trippostcollect-crawl-to-source-exhaustion` 固定使用 `source-exhausted`，且只能由用户明确要求触发。

模式 Skill 只负责选择 runner 参数与完成谓词，不拥有第二套平台、分页、登录或入库实现。

## 项目主题范围

项目只收集青岛相关数据。操作人或 Agent 在冻结正式配置、`--recovery-keyword`、诊断执行和页面
证据任务时，应确认关键词与青岛相关，通常使用“青岛…”或“崂山…”。平台 registry 的诊断默认面
和带回跳参数的登录落点使用青岛检索面；纯登录页本身不承担主题表达。

主题范围是操作约定，不是配置解析器、抓取器或导入器中的关键词硬门禁。代码不根据关键词前缀、
包含关系或正文地名拒绝执行，也不恢复 `web_posts.city_name`。平台检索可能返回噪声，保留与否仍
服从正式字段、图文类型和来源证据门禁；固定 URL 页面证据由操作人确认 URL 与所声明主题相符。

## 唯一入口

通用平台正式任务只通过 `scripts/crawl_runner.py` 执行；小红书正式任务只通过
`scripts/xhs_runner.py` 执行。其他入口的定位如下：

- `mediacrawler_crawl.py`：调度器调用的结构化执行器，也可用于 `--no-import` 诊断。
- `info_collection_benchmark.py`：通用平台性能和容量评估，不代表正式轮次完成，也不接受小红书。
- `ctf_resource_crawl.py`：固定 URL 页面证据执行器。当前正式配置没有此类任务，直接运行只用于
  开发或诊断验证；以后若配置 `job_kind=ctf_resource_crawl`，正式执行必须由调度器调用。

任何入口使用 `--no-import` 都是诊断运行。即使摘要或冻结状态因执行与产物校验通过而显示
`completed`，没有执行真实 SQLite 持久化就不满足正式完成契约。默认数量模式必须核对实际
`inserted_rows` 达标；显式来源耗尽模式允许 `inserted_rows=0`，但仍须核对持久化阶段完成和实际
计数。不得用 `import_new_target_met`、退出码或状态文件替代对应校验。

五个平台的正式结构化 child 命令必须由 runner 固定注入 `--download-images` 和
`--media-root <项目内 data/media 的绝对路径>`。`--download-images` 表示“下载并验证权威正文图”，
不启用视频；旧兼容参数 `--get-media` 对操作人不可用且会直接失败。直接调用执行器做诊断时可以
显式使用 `--download-images --media-root temp/<任务目录>`；`--no-import` 只保留 staging、manifest
和根项目字节复验，不晋升长期文件、不写 SQLite、不提交发现 checkpoint。正式入库未显式启用
`--download-images` 必须在访问平台前拒绝，任何平台都不能完成 URL-only 正式任务。

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

小红书额外使用账号隔离、加密 storage state、单账号租约和 `xhs_guarded` 行为门禁；不自动换号、
绕过验证或把登录/频控/封禁降级为候选失败。互动默认关闭，点赞等真实副作用只有操作人显式启用。
登录恢复、标签页保护、等待时间和互动证据的唯一操作说明见
[`platforms/xhs.md`](platforms/xhs.md)，其机器结果仍必须满足本文的运行级阻断和冻结状态门禁。

通用平台的验证码、频控或拒绝访问会写入站点冷却，后续任务由同一策略门禁停止；小红书只
记录本轮证据，等待操作人指挥。断点续跑只校验本次新执行记录的行为与策略证据，不要求历史
摘要补造新字段。

## 数量定义

完成模式分为两种：

- `target-new-posts` 是正常默认模式。数量目标、候选硬上限和停滞批次均生效，只有新增与实际入库
  达到配置目标才完成。
- `source-exhausted` 是用户明确要求某一轮时才启用的临时模式。它只由 runner 的
  `--completion-mode source-exhausted` 注入当前进程，不写回配置；dry-run 与正式运行必须使用相同
  参数。该模式不以 `target_new_posts`、`candidate_hard_limit` 或停滞批次作为完成/停止门禁，而以
  平台返回可验证的来源耗尽证据为完成条件。超时、登录/验证阻断、字段 profile、行为/策略证据和
  实际入库要求仍然有效。

CLI 省略参数时仍默认 `target-new-posts`；模式 Skill 在 dry-run 和正式运行中显式传入选定值，
便于冻结状态审计。`completion_mode` 不属于来源查询参数，不进入查询指纹；两种模式切换时继承
同一任务的 checkpoint、累计摘要和候选记忆，不得清空进度或重扫已处理候选。

- `candidate_hard_limit`：单次 child 执行允许进入字段校验的未知原始候选上限，不是跨多次
  续跑活动的总预算，也不是底层请求参数的同义词。数据库、当前累计摘要、对应平台的持久候选
  记忆表或本次已见集合中已知的平台 ID 会在详情、作者补全和媒体处理前跳过，不消耗该预算。
- `target_new_posts`：正常默认模式下，本轮必须取得并实际新增到 SQLite 的唯一有效图文数。
- `valid_new_count`：完成视频过滤、平台 ID 去重、必需字段校验和作者字段补全后，且
  SQLite 中不存在相同平台 ID（缺失时按规范 URL）的记录数。
- `valid_existing_count`：本次产物中完成字段校验、但 SQLite 已有对应记录的数量；它们不计入
  `target_new_posts`。发现阶段提前识别并跳过的已知 ID 不进入本计数。
- `processed_rows`：执行过入库 upsert 的行数，不表示新增数或有效数。
- `inserted_rows` / `updated_rows`：数据库新增和更新数量，必须分别报告。
  `updated_rows` 表示相同平台 ID（缺失时用规范 URL）已存在，本轮用最新字段覆盖该行并
  重建其图片关系；它不是额外新增记录，也不表示平台内容一定发生过编辑。

候选累计从 0 开始，按平台实际分页逐个增加。正常默认模式在达到 `target_new_posts`、来源明确
耗尽、连续停滞、运行超时或触及 `candidate_hard_limit` 时停止；临时 `source-exhausted` 模式忽略
新增目标、候选硬上限和停滞停止条件，只在来源明确耗尽或出现运行阻断时停止。`candidate_hard_limit` 不是要求底层
预取或处理的固定数量。小红书提高永久正式目标时必须同时核对候选上限、`max_stagnant_batches`、
`top_refresh_max_pages`、`timeout_seconds` 和账号 `lease_seconds`；租约必须至少覆盖超时加 300 秒。
这些运行预算不属于查询来源参数，调整后继续使用原目标、账号和查询指纹对应的 checkpoint，
但必须通过新的 dry-run 冻结并核对实际计划。

正常默认模式只有 `valid_new_count >= target_new_posts` 才能进入入库阶段，并且实际
`inserted_rows >= target_new_posts` 才能标记 `import_new_target_met=true`。显式
`source-exhausted` 模式只有 `formal_validation.source_exhausted_met=true` 才进入入库；此时新增数
可以低于目标甚至为 0，`new_target_met` / `import_new_target_met` 不作为完成门禁，但仍必须真实执行
持久化阶段并分别报告处理、新增和更新。任何模式都不能用退出码、`processed_rows`、
`updated_rows`、少量样本或历史数据库总量替代相应门禁。

## 有效记录

结构化图文记录至少满足：

- 有平台原始 ID 或规范 URL，且本轮唯一；
- 不是视频记录；
- 有平台权威正文字段；标题、搜索摘要和预览文本不能替代正文；
- `content_detail_status=detail_observed`，且 `content_detail_source` 属于当前平台受信任来源；
- 有平台原始发布时间；
- 有作者平台 ID 和作者昵称；
- 有至少一个正文图片 URL；
- 满足任务配置指定的作者粉丝量策略和平台字段 profile。

“正文图片”只能由以下平台权威字段显式投影，顺序去重后每项角色固定为 `content`：B站详情
`image_urls`、微博 `mblog.pics` 归一后的 `image_list`、小红书笔记详情 `image_list`、抖音图文
`note_download_url`、知乎正文/详情 `image_list`。通用递归 URL 搜索不属于正式能力。作者头像、作者
主页资源、搜索预览、封面、视频、音乐和知乎公式图片均不得生成正文候选、下载任务、manifest
行、长期文件或任何图片关系；B站搜索预览图尤其不能替代详情正文图。作者头像不是可选图片角色，
不得形成下载请求、JSONL/摘要字段、子进程日志内容、`web_posts` 字段、JSON 值或
`web_post_images` 关系。导出与入库边界必须递归删除 `avatar_url`、`author_avatar`、
`author_avatar_url`、`avatar`、`user_avatar`，并删除
同一记录内与这些键下 URL 完全相同的重复值；不得按域名、路径或文件名推测头像。

平台特有的 URL/资产键归一规则写在对应平台文档；所有平台都必须保留原始来源证据，且不得使用
感知哈希或视觉相似度改变正式候选集合。

每条进入正式有效集合的记录还必须满足整帖图片原子条件：所有权威正文图都已使用当前平台会话
下载到 staging；`image_manifest.jsonl` 的平台、帖子、角色、顺序、来源字段、稳定资产键和 URL 与
根项目投影完全一致；根项目重新验证文件边界、SHA-256、真实 MIME、后缀、尺寸和解码；正式模式
在同一帖子内按验证后的文件 SHA-256 保留源顺序首次出现项，重复来源仍保留在 manifest 和首项的
`local_file.sha256_duplicate_sources` 证据中；保留项从 0 连续重编号后晋升到
`data/media/<platform>/<safe_post_id>/<index>-<asset_hash>.<ext>`，并在同一 SQLite 事务写入帖子和带
`local_path/width/height/mime_type/sha256` 的图片关系。SHA-256 去重只表示字节完全相同，不合并仅在
视觉上相同但缩放、转码或重新编码后哈希不同的文件，也不跨帖子合并图片关系。任一图片缺失、失败或
不一致时整帖不得入库；先写 URL、以后再补本地路径不满足本契约。

平台 child 对单张正文图的 `None`、HTTP 200 空字节、超时、HTTP 408/425/5xx 或其他临时请求错误最多执行 3 次有限指数
退避重试。客户端不得把 `raise_for_status()` 的 HTTP 状态折叠为 `None`：明确终态状态至少保存真实
`http_status` 和 `image_source_unavailable`，临时状态在耗尽后保存 `image_download_retryable`；明确
非重试 HTTP、非图片、解码失败或过大等终态错误不做无意义重试。候选帖详情、作者必需字段或任一
正文图在适用尝试结束后仍失败时，必须写 `candidate_skipped` 事件；图片失败还必须先写失败 manifest。
事件至少包含平台、候选 ID、`failure_scope=post|image`、错误码、是否可重试、实际尝试次数、可用的
图片来源序号和当前 page/offset/cursor。该整帖不得写入正式 JSONL 或内容数据库，也不得把已成功的
图片子集解释为完整帖子；失败 ID 作为“已处理但未成功”写入同作用域 seen，child 继续后续候选，
checkpoint 按最后完整批次正常推进。`candidate_skipped` 不增加有效新增数，但不阻断后续
`target_new_met`、候选上限、停滞或真实 `adaptive_search_stopped(source_exhausted)`。
其中 HTTP 401/403 属于登录或授权阻断，HTTP 429 属于频控阻断，必须保存为运行级
`image_auth_required` / `image_rate_limited` 并立即停止，不能写 `candidate_skipped` 或 seen。HTTP 400/404、
格式、解码、大小等候选自身终态图片错误保留具体错误码和 `retryable=false`，不强行补足
3 次请求；临时请求错误完成有限重试后保留 `retryable=true`。操作人批准的预请求排除必须精确到平台、job、
查询指纹和候选 ID，并把原因、授权 run 与既有失败证据写入独立的
`crawl_discovery_candidate_exclusions`；该表不属于内容或已处理候选记忆。后续 child 仅在昂贵详情、
作者或图片请求前把对应 ID 视为已知并跳过，不生成 `web_posts`、不增加成功数，也不单独构成
`source_exhausted` 证据。普通重试耗尽使用自动 `candidate_skipped`，不自动创建排除表记录；排除表只用于
操作人希望在请求前精确阻止某候选的场景。若 child 在预算超时或可捕获中断前已经写出
`candidate_skipped`，根执行器仍必须从事件流聚合 `skipped_candidate_failures`；正式摘要保留
`runtime_failed`，checkpoint 只采用最后一个完整分页批次，不因已记录跳过候选回卷，也不得越过未完成
尾批。refresh 阶段记录的跳过候选同样进入 seen，不把深层前沿改成第 1 页。

历史详情修复不写 discovery 事件或 seen，但必须提供同等可审计的逐目标失败证据。通用修复 child 以
冻结目标集合减去最终正式有效集合生成 `skipped_candidate_failures`，至少保存平台、平台帖子 ID、
稳定错误码、失败范围、当前轮实际目标尝试次数和 `evidence_source`；平台子进程没有暴露更细错误时使用
`repair_target_no_valid_output` 与 `repair_target_output_difference`，不得伪造详情或图片错误原因。
`repair_import_met=true` 只允许已经通过全部字段、图片和行为门禁的成功子集入库；只有冻结目标全部有效、
失败清单为空时 `completion_met=true`。父入口必须再次按 SQLite 状态计算恢复数与残留数，并汇总
`candidate_failure_count`；整轮零恢复且没有运行级或持久化阻断时使用
`post_detail_repair_no_progress`，不得误报为 `post_detail_repair_persistence_not_verified`。

分页证据为 `runtime_failed`、`login_required`、`captcha_detected`，或本轮任一目标 child 未成功完成时，
运行失败门禁必须覆盖已经达到的数量目标：`completion_met=false`，不得晋升图片或进入 SQLite 事务。
组合门禁失败时也必须保留这一优先级：已有 `runtime_failed`、`login_required` 或
`captcha_detected` 不得被图片不完整、行为证据或策略证据的较低级停止原因覆盖。

五平台正文来源是正式字段契约，不是调试信息：

| 平台 | 受信任 `content_detail_source` | 正文字段 | 不可降级的来源 |
|---|---|---|---|
| B站 article | `article_view_api` | `content_text/content` | 搜索 `desc`、预览图 |
| 微博 | `search_mblog_complete`、`mobile_detail` | `content_text/content` | 长文搜索截断 `mblog.text`、标题 |
| 小红书 | `note_detail` | `desc` | 搜索卡片摘要、标题 |
| 抖音 | `aweme_detail` | `desc` | 标题、封面或预览文本 |
| 知乎 | `search_content`、`answer_detail`、`article_detail` | `content_text/content` | `title`、`desc/excerpt` |

平台详情、作者与粉丝的具体来源由 `required_fields_profile`、
[`platform-field-coverage.md`](platform-field-coverage.md) 和对应平台文档共同限定。五个平台当前均为
`followers_policy=required`；真实 0 必须同时具有原始字段与 `followers_observed=true`，缺失值不得
转换为 0。详情或作者候选级失败服从上面的 `candidate_skipped` 规则，登录、授权、频控、验证码和
风控仍属于运行级阻断。

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

本地图片证据跨越其中两阶段：`artifacts_verified` 必须重新读取 manifest 并验证聚合哈希、候选帖
数、完整帖数和图片计数等式；真实入库的 `persistence_verified` 必须逐帖读取 SQLite，验证连续
`image_index`、非空项目相对 `local_path`、文件仍在 `data/media` 内、文件 SHA/MIME/尺寸与数据库
一致，并通过 `PRAGMA quick_check` 与 `foreign_key_check`。`--no-import` 诊断可以在
`expect_promotion=false` 下完成产物验证，但持久化阶段只能明确跳过，不能据此完成正式任务。

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
- `stagnated`：连续配置批次没有产生平台定义的新候选或有效新增。批次必须记录
  `stagnation_basis`、新候选 ID 数和有效新增数；B站、微博、抖音、知乎和小红书各自的候选身份、
  cursor 与停滞例外只在对应平台文档维护。停滞永远不能代替 `target_new_met` 或
  `source_exhausted`。
- `login_required` / `captcha_detected`：登录或验证阻断。
- `runtime_failed`：浏览器或本地运行环境失败。
- `image_materialization_incomplete`：已选入正式有效集合的正文图 manifest 或 staging 字节校验不完整。
  身份、格式、哈希和路径边界错误必须先修复代码或产物，不能降级为 URL-only；候选请求阶段已完成
  适用重试的帖子或图片失败不进入该集合，而是记录 `candidate_skipped` 后继续。

默认 `target-new-posts` 模式只有 `target_new_met` 可以汇报完成；显式 `source-exhausted` 模式只有
`source_exhausted` 且 `source_exhausted_met=true` 可以汇报完成。其他停止状态均不能完成。固定 URL
页面任务的成功仍只代表该页面证据完成，不代表平台批量目标完成。

每个分页批次必须记录平台页码、请求游标或 search ID（平台提供时）、下一游标、可恢复的
下一页/offset/cursor、批次是否完整、发现阶段、原始返回条数和 `has_more`（平台提供时）。
抖音搜索响应还必须验证 HTTP 可解析后的业务 envelope：非成功 `status_code`、缺失或非列表
`data`、缺失或非法 `has_more`，以及 `has_more=true` 但没有下一 `logid`，都属于
`runtime_failed`，不得降级成空页。状态文件保存脱敏的 `douyin_search_response_observed`；
新鲜第 1 页为空时另存 `douyin_empty_first_page_checked`，只记录计数、匹配到的可见无结果标记、
URL 和检查错误，不保存完整响应或整页文本。
仅有一条或多条 `adaptive_batch_completed`、但没有
`adaptive_search_stopped` 的任务，不得推断为 `source_exhausted`；任何模式都按 `runtime_failed`
处理。默认数量模式的分页循环以实际候选累计到 `candidate_hard_limit` 为边界，不得用
“页数 × 名义页大小”提前截断；显式来源耗尽模式不使用该数量边界。

## 持久化发现记忆与跨次累计

B站、微博、抖音和知乎的正式 `mediacrawler_search` 任务由通用调度器自动维护发现记忆。SQLite
`crawl_discovery_checkpoints`、`crawl_discovery_seen_candidates` 与操作人授权的
`crawl_discovery_candidate_exclusions` 均以 `job_id + query_fingerprint` 隔离；前者保存安全前沿，
第二张表保存已完成处理的候选 ID，第三张表保存精确、可审计且不计成功的排除 ID。小红书由独立 runner
维护 `xhs_discovery_checkpoints`，以 `target_key + account_id + query_fingerprint` 唯一定位，
并在同一作用域的 `xhs_discovery_seen_candidates` 保存已完成处理的候选 ID；两者不与通用任务或
其他账号共享未入库活动。指纹包含平台、关键词和影响来源结果的查询参数，
不包含候选上限、目标数、超时、登录和顶部刷新页数。关键词或来源查询参数改变时必须形成新
记忆，不得误用旧游标。

通用平台在昂贵处理前跳过 `web_posts` 已有 ID、累计摘要中的有效 ID、
`crawl_discovery_seen_candidates` 已完成处理 ID、`crawl_discovery_candidate_exclusions` 中经操作人
明确授权的 ID 和当前 child 已完成 ID；小红书读取独立的
`xhs_discovery_seen_candidates`。五个平台的视频、已由决定性权威响应证明的字段无效项、有效候选，
以及详情、作者或正文图在适用重试结束后仍失败的 `candidate_skipped`，都在 child 摘要形成后获得跨轮
记忆。跳过候选不回填内容、不伪装字段无效、不增加完成计数；它只表示该来源候选已按本轮策略处理。
操作人若需在请求前阻止精确候选，可另写排除表；排除同样不增加完成计数。进程在写出候选级决定性
事件前崩溃的候选不会被提前标记；已写出的 `candidate_skipped` 只有在所属批次完整时才随分页证据
提交，未完成尾批仍允许保守重取。两套表的作用域不同：通用平台按 job 与查询
指纹隔离，小红书还按人工指定账号隔离，不能跨账号共享未入库活动。

“字段无效”必须已有决定性来源证据。B站 article、微博长文、小红书笔记和知乎 answer/article
的详情请求、空响应或解析失败先完成平台规定的有限重试；仍失败即记录 `candidate_skipped` 并写入候选
记忆，失败页可在批次完整后推进，但该 ID 永远不进入有效内容集合。登录、验证码、安全限制、频控、
搜索请求失败或浏览器整体故障仍是运行级失败，不得按候选跳过。数据库已有 ID 的历史内容修复仍必须
走平台文档定义的独立流程。

首次执行从来源第一页开始，不做顶部刷新。每个完整前沿批次把下一页写入状态事件；抖音还必须
同时写入下一 offset 和响应 search ID，小红书保存本次搜索使用的 client search ID。对应根执行器
在 child 摘要形成后才把状态事件提交到 SQLite，不得由底层循环提前推进数据库游标。中途停止的
批次保存当前请求位置，下一次允许重取该批次，依靠已知 ID 提前去重；这样可以重复少量边界
数据，但不能跳过未持久化候选。小红书只有在 child 摘要形成后才把本轮候选 ID 与前沿一起提交；
视频、已有决定性证据的字段无效项、有效候选和 `candidate_skipped` 都会获得发现记忆。进程在候选级
决定性事件或摘要形成前崩溃的候选不会被提前标记为已处理。

存在 checkpoint 时，runner 自动把保存位置传给 child；有未完成累计摘要时再传入上一份
`summary.json`。child 先刷新
配置的 `top_refresh_max_pages` 个顶部页面，再从保存前沿继续。顶部刷新用于发现新近发布内容，
不推进深层 checkpoint，也不累计前沿停滞；已知 ID 在详情、粉丝和媒体处理前跳过。深层批次
完整结束才推进到下一页；抖音恢复命令必须同时包含 `--start-page`、`--start-offset` 和非空
`--start-cursor`，小红书深页恢复必须同时包含 `--start-page` 和非空 `--start-cursor`。只有
页码没有 search ID 的请求是无效恢复。

默认数量模式下，未达到目标的产物不单独入库。runner 保存其摘要路径，下一次将历史与本次 JSONL
合并校验，并只向底层下发剩余新增目标；`candidate_hard_limit` 每次 child 执行重新提供完整预算，
不从历史候选数中扣减。累计 `valid_new_count` 达到完整 `target_new_posts` 后才一次性入库并清空累计
摘要。显式来源耗尽模式改为在本轮取得可验证耗尽证据后入库，不等待数量目标。checkpoint 本身
保留，供下一次定时任务继续向后发现。前沿推进但默认模式本次尚未达标时不增加连续失败，下一次
按任务正常调度间隔运行；无推进的运行错误仍按重试策略处理。

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

## 正文图片摘要与错误契约

执行器 `summary.json.image_materialization` 是五平台统一的公开图片结果，必须包含：

- `required`、`promotion_required`、`candidate_posts`、`complete_posts`；
- `expected_images`、`downloaded_images`、`validated_images`、`unique_images`、
  `sha256_duplicate_images`、`sha256_duplicates`、`reused_images`、`promoted_images`、
  `rolled_back_images`；
- `retryable_failures`、`terminal_failures`、`complete`；
- `manifest_paths`、`manifest_sha256`、逐文件 `manifest_evidence` 和 `failures`。

根项目必须先用 `promotion_required=false` 只读复验全部 staging/manifest，并同时完成数量或来源耗尽、
字段、行为与策略门禁；任何门禁未通过时不得写 `data/media`，摘要写
`promotion_deferred=true` 和具体 `promotion_deferred_reason`。只有这些门禁全部通过，正式运行才以
`promotion_required=true` 再次复验并晋升，然后进入 SQLite 事务。晋升函数在当前文件写入失败时
删除该文件；整轮后续图片、晋升阶段的可捕获进程中断或能够确认发生在 SQLite 提交前的导入失败时，
根执行器先回滚本轮所有 `reused=false` 的新文件。晋升阶段中断随后继续传播；进入 SQLite 外层事务
后、成功提交前的可捕获中断则归类为提交前导入失败，转成 `sqlite_import_failed` 公开结果而不再传播。
不得删除此前已存在且 `reused=true` 的内容寻址文件。提交前失败必须在
child 摘要写入 `import_result.reason=sqlite_import_failed`、错误和 `rolled_back_images`，并跳过本轮
发现 checkpoint；通用与小红书 runner 均须执行该门禁，且 discovery 提交函数必须再次防御。
所有正式轮次从首次长期媒体晋升开始，直到 SQLite 提交成功或本轮新文件回滚完成，都持有同一跨进程
媒体持久化锁。回滚删除前还要查询当前 SQLite 的 `web_post_images.local_path`，已被任一已提交帖子
引用的路径不得删除。schema bootstrap 完成后、处理第一帖前必须显式开启批次外层事务；逐帖
SAVEPOINT 只允许隔离单帖写入，不能因 `RELEASE SAVEPOINT` 提前提交。本轮任一帖子失败时，外层
事务回滚全部帖子和图片关系，公开摘要的 0 行必须与数据库实际 0 行一致。SQLite 已成功提交或提交
结果不能安全判定时，长期文件可能已形成正式引用；后续
中断、checkpoint 或报告验证失败不得删除这些文件，而应保留并核对 SQLite 后恢复控制面状态。产物完整的等式为
`candidate_posts == complete_posts` 且
`expected_images == downloaded_images == validated_images`，并且失败数与 `failures` 均为 0。正式
入库还要求 `unique_images + sha256_duplicate_images == expected_images`、
`len(sha256_duplicates) == sha256_duplicate_images`，以及
`promoted_images + reused_images == unique_images`；诊断模式要求
`promotion_required=false`，两项长期文件计数保持 0。执行器同时写
`formal_validation.image_materialization_complete=true`；多平台收集的正式校验必须满足
`local_images_complete=true`、`local_image_failure_count=0`。以上谓词还要与原数量/来源耗尽、
字段、行为、策略、分页和真实入库谓词同时成立，不能相互替代。

历史 manifest 的平台兼容只属于入库复验，不改变当前图片契约；现行条件与失败关闭规则见
[`data-persistence.md`](data-persistence.md) 和对应平台文档。

manifest schema v1 的稳定校验错误包括：`missing_image_manifest`、
`image_manifest_identity_mismatch`、`image_manifest_count_mismatch`、
`image_manifest_metadata_mismatch`、`image_path_escape`、`image_file_missing`、
`image_non_raster_response`、`image_decode_failed`、`image_too_large` 和
`image_hash_mismatch`、`image_existing_conflict` 和 `image_promotion_conflict`。缺少统一摘要使用
`image_materialization_missing`，不完整摘要使用 `image_materialization_incomplete`；平台批次停止
细节使用 `image_download_failed`，下载失败行使用稳定的 `image_download_retryable` 或
`image_non_raster_response`、`image_decode_failed`、`image_too_large`、`image_source_unavailable`
等终态错误码。除登录、授权、频控、风控、安全限制等运行级阻断外，在适用尝试结束后仍失败的
帖子或图片候选都产生候选级 `candidate_skipped` 并继续当前 child；图片临时错误
`image_download_retryable` 标记 `retryable=true`，其余候选自身终态错误标记
`retryable=false`。事件和停止摘要必须保存 `skipped_candidate_count` 与
`skipped_candidate_failures`，图片摘要仍按错误码分别汇总 retryable/terminal 计数，但跳过项不作为
图片完整集合的失败行参与晋升。
晋升/数据库文件一致性错误由 `failures[].code/message`、runner 阶段失败原因和报告共同保留。
操作人不得编辑 manifest、摘要或冻结状态把错误补签为成功。
