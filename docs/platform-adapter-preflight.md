# 平台适配 P00：正在进行的工作单与证据索引

本单记录 v0.4 前置准备，不重复定义正式契约，也不是迁移完成报告。
设计边界见 [平台实现解耦设计](platform-adapters.md)，版本操作见 [版本管理](version-control.md)，
现行行为仍以 [文档入口](README.md) 所指权威文档为准。工作单完成后按项目文档归档规则处理。

## 范围、基线与授权

- 首期必须五平台全部保持现有正常抓取能力。内部可分步，单路径验证不构成首期交付，
  未迁移平台保持原正式链路，不能逐站减损功能。
- 统一规则与结果，保留各站执行流程；只补明确缺口，不建平行框架。助手负责技术评审，
  外部选型不重开、新候选不安装。
- 当前仅授权文档、保护性测试准备与隔离验证。没有采集实现、真实采集或治理解冻授权；
  即使 P00 准备就绪，也不能自动进入 P01。
- 当前分支 `chore/platform-adapter-preflight`；执行测试时的生产源码基线为
  `f2b0d3506e336bf15dbd1416f009bf8f3b09ac5f`，不是当前 HEAD；
  子模块 HEAD `2bcde689cc9a1b50cbcc7255597e9da1ff1a1b30`，已只读复核。
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

以下版本来自直接调用相应解释器的既有检测；本次评审不重跑环境探测、不改两套环境：

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
| P00-D 隔离验证 | 已执行并复算 XML；宿主相关失败仍未全部闭合 | 下节区分通过、失败与证据限度，不把失败统一归为环境或产品缺陷 | 丢弃隔离临时产物，不接触生产状态或环境 |
| P00-E 技术评审 | 文档与测试准备可接受；P00 整体未就绪 | 宿主基线、准确接口和闭包继续补证；就绪与实施授权分列 | 保留现有五站实现，禁止自行进入 P01 |
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

### 失败分类与未决项

| 分类 | 根 3.12 数量 | 已知证据与处置 |
|---|---:|---|
| XHS 进程枚举失败 | 20 | message 含 `cannot enumerate processes for XHS lease safety`；独立 probe 的 stderr 明确 sandbox 阻止执行 /bin/ps。保留失败，不能据此宣称相关宿主行为已验收 |
| Unix socket | 1 | `test_socket_is_rejected_without_blocking` 在 deny network* 下 PermissionError；限制包括本地 socket |
| 历史 profile 扫描 | 1 | `test_xhs_schema_removes_persistent_fields_without_erasing_history_or_files` 报 legacy_profile_scan_unavailable；具体因果仍待闭合 |
| 退出码/lease_released 断言 | 7 | 疑与宿主隔离相关，尚未完全闭合；不能统称环境问题，也没有证据据此判为产品缺陷 |
| fork 3.11 额外缺包 | 另 1 | `test_all_script_run_ids_include_microseconds` 导入缺 scrapling；不证明 Python 3.11 语法不兼容 |

7 个未决节点为：`test_xhs_operator_interrupt_finalizes_state_summary_and_exact_cleanup`
的 lease_signal/keyboard_interrupt 两例、`test_xhs_repair_interrupt_writes_terminal_audit_before_exact_cleanup`
的同两例、`test_formal_os_signal_immediately_after_acquire_has_one_failed_terminal_commit` 的 [2]/[15]，
以及 `test_sms_terminal_before_pagination_keeps_precise_reason_and_checkpoint`。
完整节点与消息保留在审计附件，不 skip/xfail、不 mock 掉安全边界凑全绿。

初次试跑为 561 通过、8 失败并提前停止，不是最终基线，不与上表重复累计。其中 1 个 pytest 测试
报告 4 个副本文件缺少 uchg（git archive 不保留该标志），不是 4 个测试失败；
随后仅在隔离副本四个已登记文件恢复 uchg 元数据（metadata）。
真实原件及登记哈希未改、未解冻。允许记录并恢复副本元数据，不能把这项操作误写成禁止事项。
最终全量结果不再含该项副本冻结标志检查失败；本次评审未另跑冻结验证，后续提交仍须执行项目要求的验证。

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

仍须闭合：宿主相关失败的因果与适用基线；五站 model/helper/store/登录/JS/动态 import/
安装资源/异常与辅助入口闭包；精确 CLI/env/wire/path/指纹接口及读取时点。
V07/V08/V11 等故障窗口不能由新增纯函数测试代替，已执行范围之外的验收按对应阶段继续保留。

首期五站全功能不退化与内部任务分步不矛盾；既有 wire、runtime、知乎签名和 XHS ACK 先于
最终内容提交的顺序保持不变。**实施、真实采集和治理解冻均未授权，不进入 P01。**
