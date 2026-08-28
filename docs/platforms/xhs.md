# 小红书正式抓取 Workflow

本文是小红书账号、登录、执行和恢复的操作权威。完成谓词、候选跳过、媒体事务等共享机器语义见
[正式抓取执行契约](../formal-crawl-contract.md)；数值只从 `config/xhs_targets.json` 和
`config/xhs_pool.json` 读取。

## 硬边界

- 正式抓取只运行 `scripts/xhs_runner.py`；账号管理和登录分别只运行 `xhs_accounts.py`、
  `xhs_login.py`。
- 不把小红书放入通用 runner、warmup、`crawl_targets.json`、benchmark 或通用策略冷却。
- pool/target 使用 schema v2，没有 `enabled` 或图片开关；显式 runner 命令是唯一启动动作。
- 必须人工传 `--account-id`；一轮内不自动选号、换号、绕过验证或放宽字段。
- 正式 child 固定下载正文图片并真实入库；`--no-import` 只用于诊断。
- 账号 profile、加密状态、租约、checkpoint、seen 和累计摘要都按账号隔离。

## 执行流

```text
检查配置与账号
  -> 必要时登记并人工登录、关闭重开复验
  -> dry-run 冻结账号、目标、互动和发现计划
  -> 人工确认
  -> 正式运行并申请账号租约
  -> xhs_guarded、顶部刷新和深层 page + search_id
  -> 详情、作者粉丝和正文图片
  -> 根项目复验、媒体晋升和 SQLite 批次事务
  -> 成功后提交账号级 checkpoint/seen/campaign
  -> 加密最新状态、删除明文、释放租约
  -> 检查顶层摘要、状态、child 摘要和 SQLite
```

## 正文与图片差异

- 普通新增抓取的正文必须来自笔记详情非空 `desc`，保存
  `content_detail_status=detail_observed`、`content_detail_source=note_detail`。既有记录修复另允许平台原生的
  无 `desc` 图文笔记：必须由本轮 `note_detail` 同时观察到非空 `title` 和至少一张详情 `image_list`
  正文图；标题仍只写 `title`，不得复制到 `content_text`，也不得使用搜索卡片标题或摘要补正文。
- 正文图只来自详情 `image_list`；每个对象按 `url_default`、`url`、`url_pre` 选择一个 URL，并用稳定
  notes 路径生成资产键。头像、作者主页图片、封面、搜索预览和视频不进入图片候选或下载链；作者
  主页仍只用于观察粉丝量等研究所需作者指标。
- 图片请求复用当前隔离账号的 BrowserContext/API Cookie，不解密第二份会话，不调用视频 store。
- 作者粉丝必须来自当前登录会话的作者主页，保存数值、`followers_observed=true` 和
  `author_followers_source=creator_profile`。笔记 `xsec_token` 不能作为作者主页凭据。
- 平台层写 `<child_artifact>/xhs/data/xhs/` 下的 staging/manifest；根项目负责通用字节复验、晋升和
  SQLite。共享重试和失败分类不在本文重复。

## 1. 执行前检查

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py list
```

继续前逐项确认：

- 操作人已指定账号；状态为 `active`，没有活动租约；`storage_state.enc` 存在且可解密。
- `target_key` 存在且关键词属于青岛；目标、候选上限、顶部刷新、停滞批次和超时符合本轮要求。
- `behavior_profile=xhs_guarded` 且使用有头浏览器。
- `lease_seconds >= timeout_seconds + 300`。
- 互动未明确时为 `none`；点赞等真实副作用必须由操作人明确选择。
- schema v2 配置没有旧 `enabled` 或 `download_images` 字段。
- checkpoint 引用的累计摘要及全部 JSONL 仍存在。

缺少任一前提即停止，不直接调用 MediaCrawler 探测或绕过门禁。

## 2. 登记与登录

新账号只登记一次：

```bash
source .venv/bin/activate
python scripts/xhs_accounts.py enroll \
  --account-id xhs-a01
```

未登录、`login_required` 或需要复验时：

```bash
source .venv/bin/activate
python scripts/xhs_login.py \
  --account-id xhs-a01 \
  --timeout-seconds 600
```

登录成功必须同时完成：可见“我”与稳定身份、保存 Cookie/localStorage 以及平台写入 sessionStorage
的标签页设备 ID/运行时指纹、关闭重开同一 profile 后仍是同一身份、AES-GCM 写入
`storage_state.enc`、账号状态变为 `active`。独立登录和正式运行使用同一真实 Chrome、原生窗口尺寸、
语言、时区、mock keychain HOME 和账号 profile；不得在两个入口间切换固定 viewport 与最大化窗口。

登录工具在整个登录和复验阶段持有与正式抓取相同的账号租约；忙碌只返回 `blocked`。可见验证页
必须置前并等待操作人，标记消失且身份恢复后才继续；每阶段最多等待命令指定超时，不自动识别或
绕过验证。`retired` 不可重新登录，`quarantine` 只能人工复验后 activate。同一平台身份不能登记到
两个槽位。

`xhs_login.py` 是单页工具，不安装正式抓取的新标签页守卫，也不顺便抓内容。

## 3. 冻结计划

dry-run 必须使用正式轮相同的账号、目标、互动参数和完成模式：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --dry-run \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --completion-mode target-new-posts
```

确认：

- 顶层为 `planned`，只有 `plan_frozen=completed`，其余阶段为 `frozen`；
- pool、target、正式契约及其 SHA-256 已冻结；
- 账号、关键词、数量、候选上限、profile、互动和有头模式正确；
- preflight 证明账号 active、无租约、密文可读且租约覆盖超时；
- discovery 与该账号 checkpoint 一致：首次 page 1、顶部刷新 0；续跑有保存的 page、非空 search ID、
  顶部刷新页数及可选累计摘要。

dry-run 不申请正式租约、不解密运行时明文、不构造 child 命令，也没有 `import_result`；这些缺席不是
失败或入库证据。

## 4. 正式运行

无互动：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --completion-mode target-new-posts
```

显式互动在一轮最多一次：

```bash
source .venv/bin/activate
python scripts/xhs_runner.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --completion-mode target-new-posts \
  --post-interaction comment-scroll
```

`comment-scroll` 只访问并滚动评论区，不采集评论；`like-one` 只在明确未点赞时点击一次；`random` 在
两者中随机选择并可能产生点赞副作用。控件普通失败只影响互动证据，频控、封禁或验证仍终止运行。

已有不完整记录只能使用独立修复入口；默认最多选 20 条，并在同一 BrowserContext 内每 5 条分批，
避免为每批重启浏览器和制造新设备会话：

```bash
source .venv/bin/activate
python scripts/repair_xhs_posts.py \
  --target-key qingdao_travel \
  --account-id xhs-a01 \
  --max-items 20 \
  --batch-size 5
```

详情 API、HTML 回退、作者资料与图片各自沿用有界请求重试；最终失败会记录真实 `attempts`、
`failure_scope` 和 `error_code`。普通候选失败不序列化空壳记录，不阻塞同批成功记录，也不阻止下一批；
登录、验证码、频控、封禁、安全限制和浏览器整体失败仍立即停止。child `summary.json` 的
`pagination_evidence.skipped_candidate_failures` 与平台记录的 `repair_report` 是失败清单，顶层
`repair_xhs_posts.py` 摘要也会转存该报告。

修复详情若省略点赞、收藏、评论或分享聚合数，只在 XHS repair 路径保留 `web_posts` 已有的对应
标准化数值；fresh detail 非空值（包括真实 `0`）始终优先。回填字段必须在记录中写
`repair_fallback_evidence.metrics.<field>.source=existing_web_posts_metric`，不能据此放宽普通新抓取
门禁，也不能用旧标题、搜索摘要或旧正文替代详情正文。

运行中不得修改 pool/target。只有用户明确要求抓完来源时，dry-run 和正式轮一起改为
`source-exhausted`；不写回配置。

## 5. 抓取中的固定行为

### 标签页与人工验证

- 正式浏览器启动后先打开 `/explore` 并确认页面壳已可见，再进入真实关键词搜索页。搜索页在导航
  `commit` 后持续无可见文本时，程序只允许执行一次受控恢复：回到 `/explore` 确认渲染，再重新进入
  同一关键词搜索页；恢复仍为空白则按 `runtime_failed` 停止，不刷新账号 profile、不换号，也不推进
  checkpoint。启动导航会把 `readyState`、DOM/正文长度、主文档状态、页面脚本错误和失败资源的紧凑
  摘要写入 behavior evidence 同目录的 `behavior_evidence.navigation.json`，白屏超时不得只凭外部关闭
  后的 `TargetClosedError` 分类。
- 正式 BrowserContext 守卫安装后出现的任何新标签页都立即置前，并从出现起至少保留 30 秒；平台
  弹页、作者主页回退、互动和验证辅助页一视同仁。正常返回、异常、Playwright 退出和最终清理都
  不得绕过。首个主页面可豁免，启动时已有的额外页仍受保护。
- 明确登录、扫码或验证码页继续执行最长 600 秒人工等待，30 秒保护期不能缩短它。
- 搜索连续性出现登录要求或图片验证时，保持当前页并写
  `operator_verification_events`；完成后刷新会话并继续，超时失败。
- 搜索 API 461/471 使用响应 `Verifyuuid`、`Verifytype` 在同一 BrowserContext 打开平台人工验证页；
  可见状态必须同时检查顶层页与子 frame；通过后还需连续两次确认已回到原路由且有可见文本，
  再刷新 Cookie 并重试原请求。`Requests too frequent` 等可见频控优先于验证页标题分类，
  立即按运行级阻断停止，不点击刷新绕过。
- 搜索 API 明确登录过期时暂停原请求，保留全部标签页并置前最新 XHS 页；可见登录 UI 与 self-info
  API 都恢复后刷新 Cookie/storage state，并重试同一来源页。
- “安全限制”、账号异常、`300011/300012`、`/website-login/error`、频控或封禁属于运行级阻断，
  立即停止，不能进入人工验证码等待或候选跳过。

### 行为、作者与发现

- `xhs_guarded` 只在真实关键词页执行；使用少量桌面滚轮并验证实际位移，不扫描隐藏 HTML 文本。
- 运行指纹必须完整，`navigator.webdriver` 不得暴露，Chromium UA 与 API Client Hints 主版本一致。
- 搜索并发为 1；搜索、详情、作者主页、翻页分别随机等待并写 `request_pacing_events`，批次间继续写
  `continuity_events`。
- 作者补全先用登录会话的无 token 请求；空结果后随机等待，再用同一 BrowserContext 打开无 token
  作者页。二维码验证先等待操作人，不能因为 HTML 已有作者数据而提前关闭。
- 成功作者结果只在本轮按作者 ID 缓存，不替代来源证据。
- 有 checkpoint 时用新 search ID 刷新顶部，再用保存的 `page + search_id` 恢复深层；顶部刷新不
  覆盖深层位置。深层耗尽后只刷新顶部。
- 未知笔记 ID 才占候选预算；数据库、账号 seen、累计摘要和本轮已见 ID 在详情前过滤。完整页保存
  下一页，页中途停止保存当前页。
- 默认模式达到有效新增目标立即停止；候选上限不是预定抓取量。连续停滞按“本批没有有效新增”累计。

## 6. 完成检查

先按[正式契约](../formal-crawl-contract.md)核对五阶段、完成模式、行为/策略、图片和真实新增。小红书
还必须确认：

- 顶层 `run_summary.json` 为 `completed`，账号租约已经释放；
- `discovery.skipped=false`，checkpoint/seen/campaign 没有提交错误；
- `continuity_ok=true` 且至少覆盖 `search_results`；
- 每条记录具有 note detail 正文、原始发布时间、作者 ID/昵称、creator profile 粉丝证据和完整
  本地正文图片；
- 视频和互动只在各自报告中，不计抓取成功；
- `--no-import`、`sqlite_import_failed` 或持久化阶段 skipped 均不能完成正式轮次。

读取顺序：

1. `data/runtime/xhs/runs/<run_id>/run_summary.json`
2. `data/runtime/xhs/execution_states/<run_id>/<target_key>.json`
3. 顶层摘要指向的 child `summary.json`
4. SQLite 新增、字段和图片关系
5. 必要时日志最后 40 行

## 7. 失败分流

| 信号 | 处理 |
|---|---|
| 启动前 `login_required` | 对同一账号运行 `xhs_login.py`，成功后开始新轮 |
| 连续性登录/图片验证、461/471 | 保持页面、人工处理、刷新会话并重试原请求；600 秒超时失败 |
| 作者页二维码 | 保持作者页置前，人工扫码后继续；超时失败 |
| 任意新标签页 | 立即置前且至少保留 30 秒；明确验证页继续最长 600 秒 |
| 详情、作者或正文图候选级失败 | 核对 `candidate_skipped` 和 manifest；整帖不入库，ID 写账号 seen，继续候选 |
| 登录、频控、安全限制、封禁 | 运行级失败；保留 page/search ID，不写候选 seen |
| manifest 身份、哈希或路径错误 | 停止晋升和 checkpoint，修复代码/产物后重跑 |
| `candidate_hard_limit_reached` / `stagnated` | 默认数量模式未完成；保留累计摘要和安全前沿 |
| `source_exhausted` | 默认数量模式未达标时仍未完成；显式耗尽模式按正式契约判断 |
| 超时或缺少停止事件 | `runtime_failed`，不能推断来源耗尽 |
| 保存的 search ID 恢复失败 | 保留 checkpoint，不生成新 ID 猜测深页 |
| 累计摘要或 JSONL 缺失 | 冻结前失败；恢复原文件或停止，不清空路径继续 |
| `sqlite_import_failed` | 不提交 checkpoint、seen 或 campaign；核对数据库和媒体回滚 |

小红书不向操作人开放手工 `--resume-summary`、`--start-page` 或 `--start-cursor`。checkpoint/seen 以
`target_key + account_id + query_fingerprint` 隔离；换号不是原账号续跑。SQLite
`resume_search_id`、dry-run `plan.discovery.resume_search_id`、child `--start-cursor` 和分页事件
cursor 表示同一个 client search ID。

页面中途 `target_new_met` 且入库成功时，checkpoint 保留当前 page/search ID，
`last_batch_complete=false`、来源未耗尽时 `status=active`；清空累计摘要但不删除前沿或 seen。

## 8. 账号与状态存储

- 每个账号使用权限 `0700` 的 `data/xhs_accounts/<account_id>/profile/`，不同账号不得共享。
- profile 是持久 Chrome 目录，不宣称整个目录应用层加密。
- Cookie、localStorage、sessionStorage 设备标识和运行时 storage state 以 AES-GCM 保存为
  `storage_state.enc`；明文 `metadata.json` 只保存账号绑定信息，不保存设备标识或 Cookie。
- 持久 profile 中仍有效的 Cookie/localStorage 是当前状态；解密快照只补充 profile 缺失项，不能用
  较旧短 Cookie 覆盖 profile。所有启动过浏览器的轮次都在关闭页面前原子刷新快照，并由 runner
  校验账号绑定和必需 Cookie 后重新加密；内容抓取失败不等于丢弃已正常刷新的会话。
- 密钥优先读取 `TRIPPOSTCOLLECT_XHS_SNAPSHOT_KEY`，否则使用 macOS Keychain 服务
  `TripPostCollect.XHS`。
- 运行时明文只存在于 `data/runtime/xhs/sessions/<run_id>/`，退出必须删除。
- SQLite 租约只防同账号并发；账号切换、quarantine、activate 和 retire 均由操作人决定。
