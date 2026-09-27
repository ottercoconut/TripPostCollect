# 平台适配 P00：正在进行的工作单与证据索引

本单记录 v0.4 前置准备，不重复定义正式契约，也不是迁移完成报告。
设计边界见 [平台实现解耦设计](platform-adapters.md)，版本操作见 [版本管理](version-control.md)，
现行行为仍以 [文档入口](README.md) 所指权威文档为准。工作单完成后按项目文档归档规则处理。

## 范围、基线与授权

- 首期必须五平台全部保持现有正常抓取能力。内部可分步，单路径验证不构成首期交付，
  未迁移平台保持原正式链路，不能逐站减损功能。
- 统一规则与结果，保留各站执行流程；只补明确缺口，不建平行框架。助手负责技术评审，
  外部选型不重开、新候选不安装。
- 前期已授权文档、保护性测试准备与隔离验证；本轮仅修订本单与设计文档，不改实现、测试、配置或依赖，
  不解冻。文档编辑子任务不操作Git提交；主助手审查后可按既有授权做本地中文提交，不推送或合并。
  没有采集实现、真实采集或治理解冻授权；
  即使 P00 准备就绪，也不能自动进入 P01。
- 当前分支 `chore/platform-adapter-preflight`；执行测试时的生产源码基线为
  `f2b0d3506e336bf15dbd1416f009bf8f3b09ac5f`，不是当前 HEAD；
  子模块 HEAD `2bcde689cc9a1b50cbcc7255597e9da1ff1a1b30`，已只读复核。
  本轮文档修订起点 HEAD 为 `9083585d724465b26007c90f90ffc1ca521874e3`，工作区起始干净。
- 准备性测试已本地提交 `d1a0ce8f79842259a5569a5474774b352466dc8a`，中文标题为
  `test(采集): 补齐五平台字段与来源保护性基线`。`main` 和子模块未改，未 push、未 merge。
- 验证与独立评审日期：**2026-09-27（Asia/Shanghai）**。v0.3 的 S23—S27 研究保留。
  当前准备涉及文档及保护性测试，没有业务源码、配置或依赖改动；未真实采集、解冻或推送。
  后续本地提交按主题拆分，commit/issue/PR 标题与正文使用中文，分支按主题使用斜杠命名，不用 `codex/`。
- 测试补丁内容标识（`tests/test_full_content_contract.py` 文件 SHA-256）：
  `ae2eb0d68a5db3cefdf14ca9f342af6c307bb5601528e419825d60fc2762c1e0`，与 candidate 副本相同。
  `git diff -- tests/test_full_content_contract.py` 的 SHA-256 为
  `297767b744992d37ba5107dd145aaca47e305cdf63df70b84ba44a9dc962ad77`；文件哈希与 diff 哈希不混称。

## 环境事实与验证缺口

以下版本来自直接调用相应解释器的既有检测；本次复评未重测版本/包、不改两套环境；有限 OS 探针见失败复评：

| 对象 | 已知事实 | 不可据此推导 |
|---|---|---|
| 根 `.venv` | Python **3.12.13**，根测试已隔离执行 | 不能用 3.12 结果代替 3.11 验证 |
| 根环境已有包 | pytest 9.1.1、Playwright 1.61.0、Patchright 1.61.2、Pillow 12.3.0、Pydantic 2.13.4 | 包存在不等于浏览器、JS 或 worker 可运行 |
| 根环境缺项 | httpx、parsel、tenacity、xhshow、PyExecJS | 仅为根环境缺项，不证明正式 worker 环境缺包 |
| fork `.venv` | Python **3.11.15**；pytest 9.0.1、httpx 0.28.1、parsel 1.9.1、tenacity 8.2.2、xhshow 0.2.0、PyExecJS 1.5.1、Playwright 1.61.0、Pydantic 2.13.4 | 直接该解释器检测，不证明正式执行器未来 `uv run python` 始终解析到同一环境 |
| 目标兼容性 | 根声明 `>=3.11`；3.11 已运行根测试及所选四站/共享 worker 测试 | 不等于五站全部依赖、JS、资源和仓库外安装闭包通过；3.11 多出的一项失败为缺 scrapling，不是语法不兼容证明 |

`tests/test_full_content_contract.py` 沿用既有模块导入，测试体只调用现有纯验证/投影。
没有新建 fixture 框架；此文件在两解释器下各 66 例通过，根环境缺包未阻止此次收集。
这只确认已执行范围，不将 worker 的 import 依赖误写为根测试的必需依赖。

## 五平台逐链路检查表

以下路径以仓库根为基准；`M/` 表示 `tools/MediaCrawler/`，`C` 表示
`scripts/mediacrawler_crawl.py`，`E` 表示 `scripts/mediacrawler_export_entrypoint.py`。
“已定位”仅指本轮定向静态读取，不等于闭包已完成或运行通过。

| 平台 | 正式入口、执行与登录 | 请求、JS 与导出接缝 | 仍待闭合及验收证据 |
|---|---|---|---|
| B站 | `crawl_runner.py` → C `_run_platform_without_policy` 提前进入 `run_bilibili_article_search`；`run_bilibili_behavior_session` 使用现有持久 profile 行为 Context；辅助登录入口为 `login_warmup.py` | C 自有 `fetch_bilibili_article_detail`/`hydrate_bilibili_article_record`、搜索/作者/图片链；详情使用 urllib，行为装载 `M/libs/stealth.min.js`；根执行器负责投影与导入，**不走上游 B站视频 core/client/store** | 行为关闭后 HTTP 所需参数、下载重试、首次序列化所有出口、辅助登录闭包待核；V09 与字段/图片/事务差分待运行 |
| 微博 | 通用 runner → C → E → `M/main.py` → `media_platform/weibo/core.py:start/search`；`WeiboLogin.begin`，辅助登录走通用 warmup | client 使用 HTTPX；core 有 `libs/stealth.min.js` 分支；E 安装微博详情 fallback；`store/weibo/__init__.py:update_weibo_note` → `WeiboJsonlStoreImplement` → 清理 writer | 长文/图片/作者补取、各登录分支、cookie 传递、fallback 触发条件与退出待核；未在已读 help 中见 execjs，不据此断言全链无 JS |
| 抖音 | 通用 runner → C → E → `M/main.py` → `media_platform/douyin/core.py:start/search`；`DouYinLogin.begin` | client HTTPX 与 `get_a_bogus`；`help.py` 模块级 `execjs.compile(open('libs/douyin.js'...))`；core stealth；E 详情 fallback；`update_douyin_aweme` → `DouyinJsonlStoreImplement` → 清理 writer | 导入时 JS 读取/编译副作用、Node 运行时、空首屏及 offset/search ID、图文过滤和图片下载闭包待核；不能为了测试 import 而启动 JS/浏览器 |
| 知乎 | 通用 runner → C → E → `M/main.py` → `media_platform/zhihu/core.py:start/search`；`ZhiHuLogin.begin` | client HTTPX，引入 `help.sign`；签名读取 `libs/zhihu.js` 并 execjs 编译，保留桥接；core stealth；`update_zhihu_content` → `ZhihuJsonlStoreImplement` → 清理 writer | answer/article、请求失败/解析失败、图片会话、反向 scripts 依赖和 JS 打包/运行时待核；不能改为页面回放或拿上游 DB store 分支替代 JSONL 正式路径 |
| 小红书 | 独立 `xhs_runner.py:build_child_command` → C → E → `M/main.py` → `media_platform/xhs/core.py:start`；`_run_qrcode_login` → `XiaoHongShuLogin.begin`，单轮临时 session | client 调用 `playwright_sign.sign_with_xhshow`；core 保留 stealth 分支；`update_xhs_note` → `XhsJsonlStoreImplement` → 清理 writer，并走独立 batch checkpoint/ACK | 签名 helper、临时资源路径、扫码状态锁存、page/context 关闭、中断 terminalizer、租约与 ACK 故障窗口待核；不纳入通用 warmup，不跨轮复用登录态 |

四站共同导出已定位 E `install_export_hook`：包装 `AsyncFileWriter` 的 CSV、JSONL、单条 JSON 方法，
首次项目序列化前调用 `sanitize_export_item`。C 构造正式 worker 时选择 `--save_data_option jsonl`，
四站 store 的 JSONL 实现分别位于 `M/store/<platform>/_store_impl.py`；`main.py` 还有结束 flush/关闭分派。
这些文本证据不能证明异常出口、manifest、控制事件、日志和关闭链全部覆盖；不预判整个 store 可删除。

## 公共接口与状态、进程归属

此表只索引现有接缝，不新增 API、ABC、统一抓取循环或另一套状态机。

| 接缝 | 当前所有者及边界 | P00 核验动作 |
|---|---|---|
| CLI/env、选站与启动 | 通用 runner 管调度，XHS root 管槽位/租约；C 构造四站 worker 命令，B站原进程执行 | 列选项、默认值、读取时点与退出码；不让新包暗读全局环境 |
| 平台记录 → 字段裁决 | C `validate_formal_record`；`published_at_for_record`、`row_for_record` 做时间/行投影 | 本轮保护测试覆盖五站 count/source/observed、时间与正文状态；投影 `status` 本身不是正式成功裁决 |
| 平台分页 → 控制事件 | 各站原 worker/core 解释游标；application 决定候选/停止与推进资格 | 保留 wire 顺序和错误传播；未穷尽闭包，V01/V02/V03/V12/V14 待验证 |
| 内容与媒体提交 | C `materialize_formal_record_images`、`import_valid_records_with_media_rollback`；数据库/文件层完成事务动作 | V08 待验证；不把 worker JSONL 写出当 SQLite 内容提交 |
| 通用发现记忆 | C `persist_discovery_checkpoint`，通用任务查询指纹隔离 | 成功批与失败安全前沿分别核验，不和 XHS ACK 合并 |
| XHS 批次发现记忆 | child `publish_batch`，root `BatchCheckpointCommitter` 与 terminalizer 掌握提交/ACK/中断线性化 | V07/V11 待验证；内容提交与已 ACK 发现提交不混淆，子进程不取得长期数据库提交权 |
| 浏览器、登录与清理 | 各站原 runtime/worker 负责所持资源；XHS root 监督唯一轮次和租约 | 登录只借用原资源；不新增第二浏览器 fallback，不把辅助入口驱动推广到全部平台 |
| 最终完成 | 现有执行器门禁与 runner 状态写入；路径引用 `trippostcollect.core.paths` | 只引用正式契约，不由本设计增设成功条件或手工修正执行状态 |

## 内部任务、验收与回退

| 单元 | 可执行的工作与当前进度 | 验收证据 / 退出门禁 | 回退边界 |
|---|---|---|---|
| P00-A 文档对齐 | v0.4、版本规则、此索引与导航已审阅；事实矛盾已修正 | 批准作为准备记录，范围与授权界限一致 | 按补丁逐段撤回，保留 v0.3 研究，不使用整文件 checkout/reset |
| P00-B 保护性测试 | 在原 `formal_records` 与参数化上补强；两解释器各 66 例通过 | 批准保护性测试准备；保留失败，不改业务代码、不 skip/xfail 掩盖 | 仅撤回新增测试/参数集合补丁，不删除原断言或 fixture |
| P00-C 闭包清单 | 上述五站活跃接缝已定位，完整闭包未完成 | 逐站补 model/helper/store/登录/JS/动态 import/资源/辅助入口及读取时点，标明真实缺口；不以静态 import 数代替闭包 | 仅修正清单；不删除运行树、不切换入口 |
| P00-D 隔离验证 | 已执行并复算 XML；29/30 红项已按证据与适用阶段复评，未重跑 | 历史结果保留；业务测试随模块迁移，OS/安全清理验证在对应边界改动或正式切换前完成 | 丢弃隔离临时产物，不接触生产状态或环境 |
| P00-E 技术评审 | 文档与测试准备可接受；P00 整体未就绪 | 五站闭包、准确接口及读取时点待齐；29/30 红项本身不是统一开工阻塞，就绪与实施授权分列 | 保留现有五站实现，禁止自行进入 P01 |
| P01—P06 后续实施 | 仅为设计中的任务索引，本轮不执行 | 实施需另获授权；每个工程单元交付旧新差分，首期最终验收覆盖五站，正式试跑与治理另有授权门禁 | 依设计 S19 按接缝回退；不回滚业务数据、不删除已 ACK 快照、不手改 checkpoint |

## 契约测试审查与验证结果

独立审查了测试 diff 及 C 的 `validate_formal_record`、`published_at_for_record`、
`row_for_record`。新增测试使用原有合成 fixture，每次取得独立记录；固定五站预期不随实现集合缩减。
原 10 例的断言保留，新增 56 例，无 skip/xfail、无安全边界 mock、无业务实现修改。

| 契约 | 实质检查 | 结论与范围 |
|---|---|---|
| 五站集合 | 实现、fixture、粉丝必需集合和测试时间映射均为固定五站 | 防止实现删站后参数化也静默缩减 |
| 粉丝证据 | 真实 0 保留；count 缺失/null、source 缺失/空、observed 缺失/false 分别拒绝 | 精确校验 valid 与错误码，不只检查函数可调用 |
| 原始时间 | 移除各站原始时间，加入 captured_at 后验证器拒绝；提取与行投影均不补抓取时间 | 只覆盖 fixture 中该时间路径，不声称穷尽全部别名、嵌套时间与时区 |
| 正文证据 | 有正文和可信来源时，request_failed/parse_failed/unobserved 仍拒绝 | 保留原可信来源、标题/摘要不可冒充正文断言 |

未发现应阻止本次测试准备的断言弱化或实质性测试缺陷。覆盖仍有限：粉丝不可信来源、全部字段
别名/组合、图片字节与事务、租约/ACK 故障窗口不是新增 56 例的覆盖结论；通过纯验证/投影测试
不能证明五站端到端能力或迁移已完成。

2026-09-27 已执行结果如下。本次独立评审读取 XML 复算计数与哈希，并比较失败节点集合，
没有重跑测试。所有下列结果 skipped=0。

| 源码/测试范围 | 解释器 | 总数 | 通过 | 失败 | 退出码 |
|---|---|---:|---:|---:|---:|
| 执行测试时的生产源码基线，未复制测试补丁 | 根 3.12.13 | 798 | 769 | 29 | 1 |
| 执行测试时的生产源码基线，未复制测试补丁 | fork 3.11.15 | 798 | 768 | 30 | 1 |
| candidate，仅增加测试补丁 | 根 3.12.13 | 854 | 825 | 29 | 1 |
| candidate，仅增加测试补丁 | fork 3.11.15 | 854 | 824 | 30 | 1 |
| candidate 契约文件（原 10 + 新增 56） | 根 3.12.13 | 66 | 66 | 0 | 0 |
| candidate 契约文件（原 10 + 新增 56） | fork 3.11.15 | 66 | 66 | 0 | 0 |
| worker 选定四站及共享 32 文件 | fork 3.11.15 | 417 | 417 | 0 | 0 |

两解释器各自 baseline/candidate 失败 node 集合完全一致，新增测试没有引入新增失败。
契约专项与全量结果有重叠，不累计成独立用例总数。worker 有 1 条 SQLAlchemy
`declarative_base()` 弃用警告；该结果不是 fork 所有测试、B站视频链或线上端点验收。

### 失败复评与测试迁移

本节取代“全部宿主失败须先清零才可开工”的总括判断，保留上表全部原始数值及未执行项。
29 个共性节点为 **20 租约 + 7 终态 + 1 旧 schema + 1 socket**；3.11 另加 1 个缺包节点，共 30。
这些是失败节点数，不是独立产品缺陷数；同解释器 baseline/candidate 失败集合一致。
证据来自既有独立 Codex 审计、四份 XML 的程序提取、旧隔离产物摘要与有限 OS 探针；没有测试重跑或新全绿。
复评附件为 `/tmp/trippostcollect-failure-review.69K4k8/codex-review.md`（110 行）及 `failures.json`；
已保存本地镜像 `.git/preflight-audit/failure-review-20260927`，含 `codex-review.md`、`failures.json`、
`observations.json` 及 manifest。它们只是复评证据索引，不是可移植运行依赖或正式流程依赖。
以下正文保留完整节点映射及判断边界，删除附件后仍可理解结论。

#### OS 对照与因果置信度

宿主用 `/bin/ps` 查询自身 PID、输出 pid 字段成功；`sandbox-exec` 即使仅使用
`(version 1)(allow default)` 规则，执行同一 `/bin/ps` 仍失败，退出 71，报 `Operation not permitted`。
这证明该受限执行上下文存在进程查询前提问题，不能归结为 `offline.sb` 某一条 deny 明确禁止 ps。
`/bin/ps` 元数据为 root setuid；尚未证明它是唯一机制，不据此给出绕过方案。
Unix socket 的 `bind` EPERM 与原规则 `deny network*` 相符，但没有完成唯一成因对照。
这些有限探针不证明租约、信号或 socket 测试已通过，也不替代目标环境的 OS 集成验证。

| 组 | 数量 | 直接证据与推断限度 |
|---|---:|---|
| 租约 | 20 | 共用 `_ps_snapshots` 枚举失败；17 个直接 RuntimeError 的 XML 有 `PermissionError ... 'ps'`，2 个因清理异常覆盖原异常导致正则不匹配，1 个 child 同因退出 1。可拆 11 个业务 fixture 节点及 9 个真实进程集成节点 |
| 终态 | 7 | SMS 日志直接证明同因；另 6 个退出码节点有新增摘要证据支持同因强推断，未逐节点重跑证实 |
| 旧 schema | 1 | `legacy_profile_scan_unavailable`；内层 errno 未保留，ps 同因仍是强推断；只涉及旧库切换，未读生产库 |
| Unix socket | 1 | fixture 在 `bind` 时 EPERM，被测读取函数尚未执行；不能据此判断拒绝 socket 的实现失败 |
| 3.11 额外缺包 | 1 | run-ID 测试导入页面证据重入口时缺 `scrapling`，未执行格式断言；不是微秒或 Python 3.11 语法 bug |

#### 全部失败节点及保留语义

下表 `L::` 为 `tests/test_xhs_leases.py::`；U 为可隔离 OS 观测的业务测试，I 为真实进程集成测试。
同一行列出多个参数时，每个参数各计一个节点；表内参数名沿用原 pytest node ID。
租约实现定位于 `src/trippostcollect/xhs/leases.py`：`LeaseGuard.close` → `terminate_owned_processes`
→ `_bounded_runtime_assessment` → `_runtime_assessment` → `assess_lease_runtime` →
`SystemProcessInspector.group_members/profile_processes` → `snapshots` → `_ps_snapshots`。
恢复分支为 `recover_orphaned_account_lease` → `assess_lease_runtime`；CLI 入口是 `scripts/xhs_accounts.py`。

| 精确节点（租约 20 个） | 分层/数 | 必须保留的业务或 OS 语义 |
|---|---|---|
| `L::test_guard_removes_runtime_session_before_releasing_database_lease` | U/1 | session 删除先于数据库租约释放 |
| `L::test_ordinary_exception_releases_without_changing_health` | U/1 | 普通异常不改变账号健康；原异常不能被误判为已正常传播 |
| `L::test_runtime_session_cleanup_failure_retains_exact_lease` | U/1 | 清理失败保留精确租约；原测试尚未走到预设删除失败验证 |
| `L::test_incomplete_runtime_session_removal_retains_exact_lease[False]`、`[True]` | U/2 | 无论删除返回值为何，目录仍在即不能释放 |
| `L::test_guard_does_not_delete_session_it_failed_to_create` | U/1 | 不删除非本轮所有的 session |
| `L::test_guard_owns_and_removes_session_after_mid_creation_failure` | U/1 | 精确 claim 后半成品清理 |
| `L::test_guard_retains_claimed_session_when_marker_no_longer_matches[tampered]`、`[symlink]` | U/2 | 标记篡改或软链时不得删除 |
| `L::test_guard_does_not_follow_preexisting_session_root_symlink` | U/1 | 不跟随 session 根软链 |
| `L::test_recovery_cli_uses_lease_id_and_accepts_missing_terminal_state` | U/1 | 精确 lease ID、缺终态时的有限回收 |
| `L::test_normal_end_releases_exact_lease` | I/1 | 真实 child 结束后精确释放 |
| `L::test_child_registration_failure_stops_untracked_process_group` | I/1 | 注册失败取消 gate，不遗留未登记进程组 |
| `L::test_signal_during_child_registration_cancels_gate_before_target_exec[2-15]`、`[15-2]` | I/2 | 首个信号优先，目标不得提前执行 |
| `L::test_keyboard_interrupt_during_child_registration_never_execs_target` | I/1 | KeyboardInterrupt 后目标不得执行 |
| `L::test_child_gate_release_failure_never_execs_target_or_leaves_process` | I/1 | gate 失败不执行目标、不遗留进程 |
| `L::test_gated_child_keeps_registered_pid_and_process_group_after_exec` | I/1 | exec 保持登记 PID/PGID；此次失败在退出清理 |
| `L::test_sigterm_stops_process_group_before_release` | I/1 | 先停止真实进程组再释放；当前 child 退出 1 而非 143 |
| `L::test_sigkill_keeps_lease_and_live_child_blocks_orphan_recovery` | I/1 | SIGKILL 保留租约，真实存活 child 阻止孤儿回收 |

| 精确节点（终态 7 个） | 数量 | 既有失败与新增摘要证据 |
|---|---:|---|
| `tests/test_xhs_pool.py::test_xhs_operator_interrupt_finalizes_state_summary_and_exact_cleanup[lease_signal]`、`[keyboard_interrupt]` | 2 | 预期 130，实际 2；摘要均已记录 operator_interrupt、SIGINT 及预期退出码 |
| `tests/test_xhs_post_repair.py::test_xhs_repair_interrupt_writes_terminal_audit_before_exact_cleanup[lease_signal]`、`[keyboard_interrupt]` | 2 | 预期 130，实际 2；摘要均已记录 operator_interrupt、SIGINT 及预期退出码 |
| `tests/test_xhs_terminalizer.py::test_formal_os_signal_immediately_after_acquire_has_one_failed_terminal_commit[2]`、`[15]` | 2 | 预期 130/143，实际 2；摘要分别已记录 operator_interrupt、SIGINT/SIGTERM 及预期退出码 |
| `tests/test_xhs_terminalizer.py::test_sms_terminal_before_pagination_keeps_precise_reason_and_checkpoint` | 1 | `lease_released=false`；原因保留 sms_verification_terminal / xhs_sms_verification_parameter_error |

六个中断节点的 `run_summary.json` 均显示精确清理尚未确认（`exact_cleanup_pending`），因此实际返回 2；
不能把退出 2 写成没有捕获中断，也不能把正确摘要当成清理已通过。六个节点 ps 同因仍是强推断。
`candidate-full-py312.log` 最后 40 行的 SMS `finalization_error` 明确为
`RuntimeError: cannot enumerate processes for XHS lease safety`，并有 `lease_retained_for_recovery=true`。
SMS 的原因断言已执行，失败后的 SQL/checkpoint 断言未执行，不能补签通过。
实现定位：`src/trippostcollect/xhs/terminal.py:XhsRunTerminalizer.finish` 调用 `guard.close()`；
清理异常时保留租约并返回未确认，`scripts/xhs_runner.py` 与 `scripts/repair_xhs_posts.py` 优先返回 2。
该组继续保护首次信号优先、终态先于清理、事件仅一次、精确释放与 checkpoint 不推进；真实 `os.kill` 两例独立验收。

| 其余精确节点 | 实现定位与验证边界 |
|---|---|
| `tests/test_xhs_pool.py::test_xhs_schema_removes_persistent_fields_without_erasing_history_or_files` | `src/trippostcollect/db/bootstrap.py:_guard_xhs_legacy_cutover` → `leases.assess_legacy_lease_cutover`；fixture 未注入已有 `cutover_inspector`，应保留字段收敛、历史事件、外键与历史文件不误删 |
| `tests/test_xhs_runtime_status.py::test_socket_is_rejected_without_blocking` | `src/trippostcollect/xhs/runtime.py` 的状态安全读取；失败在 fixture bind，被测函数未执行，拒绝 socket 且限时返回仍须验收 |
| `tests/test_run_ids.py::test_all_script_run_ids_include_microseconds`（仅 3.11） | 非参数化循环到 `ctf_resource_crawl.utc_stamp`；`scripts/ctf_resource_crawl.py` → `scripts/ctf_scrapling_preflight.py` 导入缺 scrapling；时间格式尚未被测 |

#### 是否影响正常使用与分阶段门禁

| 组 | 对正常使用的判断 | 验证与门禁所在阶段 |
|---|---|---|
| 租约与终态清理 | 未证明有权限的宿主正常抓取回归；若真实目标环境同样不能枚举进程，正常收尾、精确清理、孤儿恢复与同账号续跑都会受阻 | 对应清理/监督边界改动时做业务观测矩阵和适用 OS 验证；正式切换前完成。目标环境确实缺少必需能力时，阻塞该环境正式运行 |
| 旧 schema 扫描 | 当前/空 schema 有快路径；旧库在同类扫描失败时会拒绝切换，统一 bootstrap 使影响可能超出 XHS；未读生产库，不能宣称生产无影响 | 旧库迁移或切换前验证；不强迫所有模块开工前完成旧库切换验收 |
| Unix socket | 正常状态为普通文件，未显示正常采集损坏；特殊文件拒绝能力此次未验到 | 修改安全读取边界或正式切换前，在支持临时本地 socket 的环境独立验收 |
| 缺 scrapling | 该 3.11 环境无法导入页面证据入口，不能外推五平台抓取均失败 | 对应入口/打包边界变更时验证，完整安装与正式切换前完成依赖及 CLI smoke；不在本轮补包 |

因此，29/30 红项本身不构成统一的重构开工阻塞，也不能无条件表述为“完全不影响使用”。
P00 当前仍未就绪，原因是五站完整闭包及精确接口、读取时点待齐；完成准备仍不等于实施获准。
OS/安全清理证据按受影响边界安排，不把所有整改前置到当前文档准备，也不把它们推迟到正式切换之后。

#### 测试必须随模块重构

冻结的是业务断言和保护语义，不冻结测试文件、私有函数、旧脚本名或 monkeypatch 架构。
当前 30 个失败节点未发现已废弃、可直接删除的保护语义；后续可移动、拆分、重写 fixture 与调用入口。
`control_db` 只隔离路径而未隔离真实 inspector，是业务测试触碰宿主的原因之一，不是要求永久保留的测试结构。

| 测试层 | 迁移方式与保留断言 | 验证时点 |
|---|---|---|
| contract + 纯解析单测 | 固定五站业务期望、字段来源、时间、错误与完成谓词；run-ID 改测未来稳定纯接口，保留微秒、格式与并发唯一性 | 对应模块迁入时随代码迁移，纯解析仍独立测试 |
| 运行控制/业务流程组件测试 | 11 个租约 U 节点及终态业务测试使用真实 Guard/terminalizer + 临时 SQLite/文件，经已有 inspector 或未来窄端口注入具体观测，保留编排与持久化断言；租约不归各站 adapter | 对应运行控制/业务流程组件迁入时随代码迁移，隔离宿主前提 |
| 各站 adapter 测试 | 仅做请求/解析/序列化/平台错误的净化 fixture 差分；按各站边界注入受控请求和平台响应，保留站点特有行为与失败传播 | 对应平台及接缝变更时完成，不能靠复制旧路径证明覆盖 |
| OS integration | 真实 child、gate、exec、信号、进程身份、精确清理、socket 分组验证；继续隔离外网、浏览器与生产路径 | 对应 OS 边界改动或正式切换前，在具备必需能力的环境验收，不能用空进程 fake 替代 |
| packaging / CLI smoke | 独立检查完整入口导入、Python 3.11/3.12、依赖/JS/安装资源闭包；纯 run-ID 测试不承担重入口导入验收 | 相关打包/入口变更及 P05 完整安装验收，正式切换前齐备 |

观测注入须覆盖“已证明无残留 / 有精确存活进程 / 身份或状态未知 / 扫描报错”；未知和报错不得伪报释放成功。
禁止全局将 `safe_to_release=True` 或 `close=True`，也不得以忽略异常、skip/xfail 或删除安全断言换全绿。
移除旧 hook 结构断言前，先以新显式出口的脱敏及失败传播测试替代，覆盖头像/同记录重复 URL 的首次序列化清理。
真实 gate、signals、socket 另做适用集成验证；业务输入可控与 OS 接入真实有效是两项不同证据。
上述均为后续迁移策略，本轮不改测试、不运行测试，不将置信度或建议记成已通过结果。

#### 两项诊断风险另列，不顺手修实现

`leases.py:LeaseGuard.__exit__/close` 的清理 RuntimeError 可覆盖原业务异常；这是实际异常传播风险，
应专门回归“原异常 + 清理异常”的优先级与可观察诊断，不能据此定案为正常抓取业务 bug。
`terminal.py:XhsRunTerminalizer.finish` 捕获清理异常后，返回对象与磁盘 provisional/pending 摘要可能不同；
需专门异常路径回归返回值、落盘摘要/状态与租约保留的一致性，不能混同为没有记录中断。
相关行为如需改变，应在对应清理/终态边界变更时另评并验证，不以文档复评授权顺手修复。

初次试跑为 561 通过、8 失败并提前停止，不是最终基线，不与上表重复累计。其中 1 个 pytest 测试
报告 4 个副本文件缺少 uchg（git archive 不保留该标志），不是 4 个测试失败；
随后仅在隔离副本四个已登记文件恢复 uchg 元数据（metadata）。
真实原件及登记哈希未改、未解冻。允许记录并恢复副本元数据，不能把这项操作误写成禁止事项。
最终全量结果不再含该项副本冻结标志检查失败；该测试审计当时未另跑冻结验证。
本轮文档修订的冻结校验单列，不改写上述测试结果，后续提交仍须执行项目要求的验证。

### 隔离与可重建步骤

基线和 candidate 均由上述执行测试时的生产源码基线与子模块 HEAD 的 git archive 构建，只有 candidate 复制测试补丁。
未复制生产 data、outputs、凭证、profile 或 venv，也未新装解释器或包；测试只读取现有 venv。
env -i 清空继承环境，HOME/TMPDIR/PYTHONPATH/TRIPPOST_PROJECT_ROOT 均指向临时区。
sandbox-exec 禁 network*、真实仓库写、生产数据与凭证/profile 读取以及浏览器执行。

下列为复建说明，**本次评审不执行**。在项目根目录，用已审阅且哈希相符的测试文件构建副本：

~~~bash
source .venv/bin/activate
PREFLIGHT_SOURCE="$PWD"
PREFLIGHT_AUDIT="$(mktemp -d /tmp/trippostcollect-preflight.XXXXXX)"
mkdir -p \
  "$PREFLIGHT_AUDIT/home" \
  "$PREFLIGHT_AUDIT/tmp"
for copy in baseline-root candidate-root; do
  mkdir -p "$PREFLIGHT_AUDIT/$copy/tools/MediaCrawler"
  git archive f2b0d3506e336bf15dbd1416f009bf8f3b09ac5f |
    tar -x -C "$PREFLIGHT_AUDIT/$copy"
  git -C tools/MediaCrawler \
    archive 2bcde689cc9a1b50cbcc7255597e9da1ff1a1b30 |
    tar -x -C "$PREFLIGHT_AUDIT/$copy/tools/MediaCrawler"
  chflags uchg \
    "$PREFLIGHT_AUDIT/$copy/docs/admin-client-record-workbench-template.html" \
    "$PREFLIGHT_AUDIT/$copy/docs/formal-crawl-contract.md" \
    "$PREFLIGHT_AUDIT/$copy/docs/crawl-architecture.md" \
    "$PREFLIGHT_AUDIT/$copy/docs/data-persistence.md"
done
cp tests/test_full_content_contract.py \
  "$PREFLIGHT_AUDIT/candidate-root/tests/test_full_content_contract.py"
~~~

在新临时目录保存 `offline.sb`，策略如下（固定生产路径与本次核验环境相符；迁移机器时须核对路径）：

~~~scheme
(version 1)
(allow default)
(deny network*)
(deny file-write* (subpath "/Users/kawauso/Documents/Projects/TripPostCollect"))
(deny file-read* file-write*
  (subpath "/Users/kawauso/Documents/Projects/TripPostCollect/data")
  (subpath "/Users/kawauso/Documents/Projects/TripPostCollect/outputs")
  (subpath "/Users/kawauso/Documents/Projects/TripPostCollect/tools/MediaCrawler/browser_data")
  (subpath "/Users/kawauso/.ssh")
  (subpath "/Users/kawauso/.codex")
  (subpath "/Users/kawauso/Library/Keychains")
  (subpath "/Users/kawauso/Library/Application Support/Google")
  (subpath "/Users/kawauso/Library/Application Support/Chromium"))
(deny process-exec
  (literal "/usr/bin/open")
  (literal "/usr/bin/osascript")
  (regex #".*(Google Chrome|Chromium|chrome-headless|/chrome|/firefox|/webkit|/MiniBrowser).*"))
~~~

同一 zsh 会话中分别运行两个副本和两解释器；每项记录独立 XML、退出码和短日志尾部。
现有解释器的绝对路径仅用于读取环境，项目导入及测试 cwd 来自副本：

~~~bash
for copy in baseline-root candidate-root; do
  for lane in py312 py311; do
    preflight_python="$PREFLIGHT_SOURCE/.venv/bin/python"
    if [[ "$lane" == py311 ]]; then
      preflight_python="$PREFLIGHT_SOURCE/tools/MediaCrawler/.venv/bin/python"
    fi
    (
      cd "$PREFLIGHT_AUDIT/$copy" || exit 1
      /usr/bin/sandbox-exec \
        -f "$PREFLIGHT_AUDIT/offline.sb" \
        /usr/bin/env -i \
        HOME="$PREFLIGHT_AUDIT/home" \
        LANG=en_US.UTF-8 \
        PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
        TMPDIR="$PREFLIGHT_AUDIT/tmp" \
        PYTHONDONTWRITEBYTECODE=1 \
        PYTHONNOUSERSITE=1 \
        PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
        PYTHONPATH="$PREFLIGHT_AUDIT/$copy/src:$PREFLIGHT_AUDIT/$copy/scripts" \
        TRIPPOST_PROJECT_ROOT="$PREFLIGHT_AUDIT/$copy" \
        "$preflight_python" -m pytest \
        -p pytest_asyncio.plugin \
        -q --tb=short \
        --basetemp="$PREFLIGHT_AUDIT/tmp/$copy-$lane" \
        --junitxml="$PREFLIGHT_AUDIT/$copy-$lane.xml" \
        > "$PREFLIGHT_AUDIT/$copy-$lane.log" 2>&1
    )
    preflight_exit=$?
    printf '%s\n' "$preflight_exit" > "$PREFLIGHT_AUDIT/$copy-$lane.exit"
    tail -n 8 "$PREFLIGHT_AUDIT/$copy-$lane.log"
  done
done
~~~

契约专项在同一隔离命令的 pytest 参数加入 `tests/test_full_content_contract.py`，
并更换独立 basetemp/XML/log 路径。worker 从 baseline 的 `tools/MediaCrawler` 为 cwd，
用 fork 3.11 解释器及同一隔离边界显式选择以下 32 个 `tests/test_<名称>.py`，
不以整个 fork tests 目录代替：

- cdp_browser、cdp_browser_lifecycle、douyin_image_only、douyin_no_user_info、douyin_search_safety、douyin_store
- image_client_http_classification、image_download_retry、image_staging_errors、trippostcollect_adaptive
- weibo_empty_search、weibo_image_download、weibo_no_user_info、weibo_store
- xhs_core_access_error、xhs_creator_enrichment、xhs_discovery_memory、xhs_image_download、xhs_login_contract、
  xhs_manual_wait_budget、xhs_media_policy、xhs_midrun_login_recovery、xhs_network_recovery、xhs_popup_guard、
  xhs_qrcode_login、xhs_qrcode_preview、xhs_raw_response_errors、xhs_shutdown_error_priority、xhs_store_provenance
- zhihu_detail_images、zhihu_image_download、zhihu_search_detail

### 当前审计附件

本地审计副本：`.git/preflight-audit/20260927-7vwrnvir`，保存 XML、日志、脚本及 manifest 哈希。
原临时附件根目录：`/tmp/trippostcollect-preflight.xYq5CQ`，不是正式运行依赖；
关键事实已在正文保留，包括版本、命令、范围、结果和失败分类，临时文件删除后仍能理解结论。
`preparation-report.md` 是验证前快照，其中“未执行测试”等措辞不能充当当前状态。
`run-baselines.sh`、`run-contracts.sh`、`run-candidate-baselines.sh` 与 `offline.sb` 保存实际运行参数。

| 附件 | SHA-256 |
|---|---|
| validation-results.json | `63bd6a4360b7f7efa5d6fdb782a751ff54c0eb546d3901895ff0352bffe6bb3e` |
| full-py312.xml | `61610aadb5d61bc3ef7bec5787c8a9432795635ba1e00df04a24b6f6bce8dc0f` |
| full-py311.xml | `5e6255faf0804e2dcd0eccab886c52a717987036923bfa07f57cad3b9ff739b5` |
| candidate-full-py312.xml | `081be50cc7757dc2f336c62c8b76be78cfb3034a650dc9f51930295a7a9e5bba` |
| candidate-full-py311.xml | `89f79b43c65991d37fc06d181af9a6d242d1a857485387a2bdc5388f03fe7157` |
| contracts-py312.xml | `e8dd2dce0ed29bcedc2e319c63825ed486b667246bd23f739069239af9ca0009` |
| contracts-py311.xml | `245a5b604929057ba35deba66f42b41d3cfa7e135c33394c2d2cd63399df087a` |
| worker-py311.xml | `3d0d74a1d997847f82e825fb5d695bc4140e12dfb741c7c195476cb93a09830a` |

## 独立评审结论与 P00 阻塞

批准文档对齐、保护性测试准备及已执行隔离验证的证据记录；不批准 P00 整体就绪。
新增测试实质检验现有契约，没有弱化原断言。测试准备的通过不构成零缺陷结论。

P00 就绪仍须闭合：五站 model/helper/store/登录/JS/动态 import/安装资源/异常与辅助入口闭包；
精确 CLI/env/wire/path/指纹接口及读取时点。29/30 红项按上述复评分组处理，不要求全部先清零才能开工。
测试随模块重构，业务保护语义保持；OS 与安全清理行为在修改对应边界或正式切换前须有适用验证。
V07/V08/V11 等故障窗口不能由新增纯函数测试代替，已执行范围之外的验收按对应阶段继续保留。

首期五站全功能不退化与内部任务分步不矛盾；既有 wire、runtime、知乎签名和 XHS ACK 先于
最终内容提交的顺序保持不变。**实施、真实采集和治理解冻均未授权，不进入 P01。**
