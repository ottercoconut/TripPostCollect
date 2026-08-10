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

小红书在此门禁外再强制执行独立控制面：账号和浏览器 profile 一一绑定；storage state
静态保存为 AES-GCM 密文，只在运行目录短暂解密；操作人必须通过 `--account-id` 选择账号；
单账号租约只防止同一账号并发使用，不实施自动账号轮换、日预算、冷却或全池熔断。启动前
登录失效进入 `login_required`。有头正式抓取中，搜索 API 明确返回登录已过期时，当前
请求必须暂停，所有标签页保持打开并置前最新的小红书页，最长等待 600 秒由操作人手工恢复；
只有可见登录 UI 与 self-info API 都重新确认有效后，才刷新 Cookie 和 storage state 并重试同一
来源页。超时才进入 `login_required`，不得推进 checkpoint。其他验证和频控信号只记录证据，
后续重试、隔离、恢复和切号由操作人决定。小红书只允许 `xhs_guarded`，不自动点击、识别或
绕过验证，不在失败中途切换账号。搜索结果、笔记
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
行、长期文件或 `image_role=content` 的图片关系；B站搜索预览图尤其不能替代详情正文图。作者头像
可以作为独立 `image_role=author_avatar` 的 URL-only 参考关系保留，但不下载、不计入
`post_images_count`，也不参与本地图片完整性等式。

知乎是唯一使用平台 URL 语义去重的正文图投影：仅对 `zhimg.com` 已知变换后缀（如 `_r`、
`_720w`、`_1440w`）归一化为同一资源路径，同帖保留首次出现 URL 后再生成下载任务。这是确定性的
平台资产键规则，不使用感知哈希、相似度或视觉模型；非 `zhimg.com` URL 不应用该规则。原始
`image_list` 仍在内容原始证据中保留，投影后被合并的变体不重复生成 manifest 行。

每条进入正式有效集合的记录还必须满足整帖图片原子条件：所有权威正文图都已使用当前平台会话
下载到 staging；`image_manifest.jsonl` 的平台、帖子、角色、顺序、来源字段、稳定资产键和 URL 与
根项目投影完全一致；根项目重新验证文件边界、SHA-256、真实 MIME、后缀、尺寸和解码；正式模式
在同一帖子内按验证后的文件 SHA-256 保留源顺序首次出现项，重复来源仍保留在 manifest 和首项的
`local_file.sha256_duplicate_sources` 证据中；保留项从 0 连续重编号后晋升到
`data/media/<platform>/<safe_post_id>/<index>-<asset_hash>.<ext>`，并在同一 SQLite 事务写入帖子和带
`local_path/width/height/mime_type/sha256` 的图片关系。SHA-256 去重只表示字节完全相同，不合并仅在
视觉上相同但缩放、转码或重新编码后哈希不同的文件，也不跨帖子合并图片关系。任一图片缺失、失败或
不一致时整帖不得入库；先写 URL、以后再补本地路径不满足本契约。

平台 child 对单张正文图的 `None`、HTTP 200 空字节、超时或临时请求错误最多执行 3 次有限指数
退避重试。客户端不得把 `raise_for_status()` 的 HTTP 状态折叠为 `None`：明确终态状态至少保存真实
`http_status` 和 `image_source_unavailable`，临时状态在耗尽后保存 `image_download_retryable`。仍失败且
错误码为唯一可暂缓码 `image_download_retryable` 时，必须先把失败行写入
manifest，再写 `candidate_deferred` 事件，至少包含平台、候选 ID、错误码、实际尝试次数、图片来源
序号和当前 page/offset/cursor。该整帖不得写入正式 JSONL、数据库或跨轮候选记忆；同一 child 内可
把该 ID 放入临时 deferred 集合以避免重复请求，并继续处理后续候选。最终停止事件必须携带完整
`deferred_retryable_failures`，恢复坐标回到本轮最早失败位置且 `batch_complete=false`。后续候选若
独立达到 `target-new-posts` 既定目标，可以只用图片完整的正式有效集合完成和入库；否则本轮保持
`deferred_retry_pending` 并保留累计摘要，不做部分入库。`source-exhausted` 模式只要仍有 deferred
候选，就必须保持 `source_exhausted_met=false`，不得把扫描到末尾解释为来源耗尽完成。
停止优先级固定为：运行失败、数量目标达成、待重试候选、候选上限/停滞/来源耗尽；因此 deferred
恰好用尽候选上限或随后扫描到空页时仍必须报告 `deferred_retry_pending`。格式、解码、大小、明确
非重试 HTTP 等终态图片错误不得写 `candidate_deferred`；写完失败 manifest 后以 `runtime_failed`
停在当前 page/offset/cursor，且失败 ID 不写正式 JSONL、SQLite 或 seen。
分页证据为 `runtime_failed`、`login_required`、`captcha_detected`，或本轮任一目标 child 未成功完成时，
运行失败门禁必须覆盖已经达到的数量目标：`completion_met=false`，不得晋升图片或进入 SQLite 事务。

五平台正文来源是正式字段契约，不是调试信息：

| 平台 | 受信任 `content_detail_source` | 正文字段 | 不可降级的来源 |
|---|---|---|---|
| B站 article | `article_view_api` | `content_text/content` | 搜索 `desc`、预览图 |
| 微博 | `search_mblog_complete`、`mobile_detail` | `content_text/content` | 长文搜索截断 `mblog.text`、标题 |
| 小红书 | `note_detail` | `desc` | 搜索卡片摘要、标题 |
| 抖音 | `aweme_detail` | `desc` | 标题、封面或预览文本 |
| 知乎 | `search_content`、`answer_detail`、`article_detail` | `content_text/content` | `title`、`desc/excerpt` |

微博 `isLongText=true` 时必须取得移动端详情正文后才能写 JSONL；详情请求、
响应或解析失败立即以 `full_text_request_failed` 阻断当前批次，保留原页为恢复
前沿，禁止把截断搜索文本交给 store。小红书笔记详情与知乎回答/文章详情的可恢复
请求、空响应或解析失败同样阻断当前批次，失败 ID 不得进入已处理候选记忆。

B站 article 还必须满足详情完整性门禁：搜索结果中的 `desc` 只允许作为发现摘要保存在原始证据，
不得作为 `content_text`；正式记录必须保存 `content_detail_status=detail_observed` 和受信任的 article
详情来源。正文图片必须经过详情响应或详情页正文结构检查，不能仅凭搜索 `image_urls` 宣布完整。
详情请求限流、超时、HTTP/业务失败或解析失败属于可恢复运行错误，不是字段永久无效。

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
- `stagnated`：抖音、知乎和小红书按连续配置批次没有新增满足正式字段 profile 且数据库中
  不存在的唯一记录累计；新的无效候选、重复候选和数据库已有记录都不能重置停滞计数。微博
  按是否出现不在数据库、累计摘要、`crawl_discovery_seen_candidates` 和本 child 已见集合中的候选 ID 累计停滞：综合搜索连续出现纯文本或视频时仍推进扫描，
  只有连续批次没有新 ID 才停；正式完成仍只计算有效新增图文。批次事件记录
  `stagnation_basis`、新候选 ID 数和有效新增数，供区分“结果类型不合格”与“页面实际重复”。
  B站自有 article 实现是另一明确例外：它按页面是否出现不在数据库、累计摘要、
  `crawl_discovery_seen_candidates` 或本次已完成集合中的 article ID 判断来源是否仍有新候选。
  未知 ID 只有在详情与字段处理得到决定性结果后才算完成处理；详情限流、超时、请求或解析失败
  必须停止当前 child、保留本页且不持久化该 ID，不能靠摘要非空重置停滞并继续跨页。详情已观察后
  才确定的永久无效候选可以重置停滞。该例外不放宽完成标准；判断 B站是否值得扩容时必须同时读取
  新 ID 数、详情成功数和有效新增数。
- `login_required` / `captcha_detected`：登录或验证阻断。
- `runtime_failed`：浏览器或本地运行环境失败。
- `image_materialization_incomplete`：正文图 manifest 或 staging 字节校验不完整。可恢复下载
  失败保留当前安全前沿，不把该候选写入已处理记忆；身份、格式、哈希和路径边界错误必须先修复
  代码或产物，不能降级为 URL-only。
- `deferred_retry_pending`：至少一个候选的正文图已完成有限重试并留证，但整帖尚未满足图片门禁。
  child 可以继续后续候选，checkpoint 必须回到最早 deferred 坐标，失败 ID 不写 seen；该状态在
  显式来源耗尽模式下不能完成，在默认数量模式未达到目标时也不能完成。

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
`crawl_discovery_checkpoints` 与 `crawl_discovery_seen_candidates` 均以
`job_id + query_fingerprint` 隔离；前者保存安全前沿，后者保存已完成处理的候选 ID。小红书由独立 runner
维护 `xhs_discovery_checkpoints`，以 `target_key + account_id + query_fingerprint` 唯一定位，
并在同一作用域的 `xhs_discovery_seen_candidates` 保存已完成处理的候选 ID；两者不与通用任务或
其他账号共享未入库活动。指纹包含平台、关键词和影响来源结果的查询参数，
不包含候选上限、目标数、超时、登录和顶部刷新页数。关键词或来源查询参数改变时必须形成新
记忆，不得误用旧游标。

通用平台在昂贵处理前跳过 `web_posts` 已有 ID、累计摘要中的有效 ID、
`crawl_discovery_seen_candidates` 已完成处理 ID 和当前 child 已完成 ID；小红书读取独立的
`xhs_discovery_seen_candidates`。五个平台的视频、已由决定性权威响应证明的字段无效项和有效
候选，都在 child 摘要形成后获得跨轮记忆。详情请求、空响应、解析或下载等可恢复失败不是
“已完成处理”；该 ID 不写候选记忆。详情失败停止当前安全批次；正文图下载最终失败写
`candidate_deferred` 后可继续同一 child 的后续候选，但最终批次仍不完整，恢复位置取最早失败的
原请求页/游标。进程在摘要前崩溃的候选也不会被提前标记。两套表的作用域不同：通用平台按 job 与查询
指纹隔离，小红书还按人工指定账号隔离，不能跨账号共享未入库活动。

“字段无效”必须已有决定性来源证据。B站 article、微博长文、小红书笔记和知乎 answer/article
的详情限流、超时、请求失败、空响应或解析失败表示候选尚未完成处理：失败 ID 不得进入候选
记忆，失败页不得推进为下一页。旧累计摘要缺少正文状态/来源时不做兼容放行；其中的 ID 不进入有效恢复
去重集，续跑时重新取得权威正文证据。数据库已有 ID 的历史内容修复仍必须走平台文档定义的独立流程。

首次执行从来源第一页开始，不做顶部刷新。每个完整前沿批次把下一页写入状态事件；抖音还必须
同时写入下一 offset 和响应 search ID，小红书保存本次搜索使用的 client search ID。对应根执行器
在 child 摘要形成后才把状态事件提交到 SQLite，不得由底层循环提前推进数据库游标。中途停止的
批次保存当前请求位置，下一次允许重取该批次，依靠已知 ID 提前去重；这样可以重复少量边界
数据，但不能跳过未持久化候选。小红书只有在 child 摘要形成后才把本轮候选 ID 与前沿一起提交；
视频、已有决定性证据的字段无效项和有效候选都会获得发现记忆；可恢复详情失败不会。进程在摘要前
崩溃的候选也不会被提前标记为已处理。

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
删除该文件；整轮后续图片或能够确认发生在 SQLite 提交前的导入失败时，根执行器回滚本轮所有
`reused=false` 的新文件，不得删除此前已存在且 `reused=true` 的内容寻址文件。提交前失败必须在
child 摘要写入 `import_result.reason=sqlite_import_failed`、错误和 `rolled_back_images`，并跳过本轮
发现 checkpoint。SQLite 已成功提交或提交结果不能安全判定时，长期文件可能已形成正式引用；后续
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

manifest schema v1 的稳定校验错误包括：`missing_image_manifest`、
`image_manifest_identity_mismatch`、`image_manifest_count_mismatch`、
`image_manifest_metadata_mismatch`、`image_path_escape`、`image_file_missing`、
`image_non_raster_response`、`image_decode_failed`、`image_too_large` 和
`image_hash_mismatch`、`image_existing_conflict` 和 `image_promotion_conflict`。缺少统一摘要使用
`image_materialization_missing`，不完整摘要使用 `image_materialization_incomplete`；平台批次停止
细节使用 `image_download_failed`，下载失败行使用稳定的 `image_download_retryable` 或
`image_non_raster_response`、`image_decode_failed`、`image_too_large`、`image_source_unavailable`
等终态错误码。只有 `image_download_retryable` 可以产生候选级 `candidate_deferred`；终态错误必须
停止当前 child。存在未清空暂缓项且本轮未满足数量目标时
停止原因为 `deferred_retry_pending`；事件和停止摘要必须保存 `deferred_retryable_count` 与
`deferred_retryable_failures`。晋升/数据库文件一致性错误由 `failures[].code/message`、runner 阶段失败原因和报告共同保留。
操作人不得编辑 manifest、摘要或冻结状态把错误补签为成功。
