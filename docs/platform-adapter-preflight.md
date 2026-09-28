# 平台适配 P00 v0.7：设计准备与证据索引

本单记录 v0.6/v0.7 前置准备，不重复定义正式契约，也不是迁移完成报告。
设计边界见 [平台实现解耦设计](platform-adapters.md)，版本操作见 [版本管理](version-control.md)，
现行行为仍以 [文档入口](README.md) 所指权威文档为准。工作单完成后按项目文档归档规则处理。

**当前状态（2026-09-28）：P00-01–08 全部完成，设计就绪 D 门禁的静态部分满足；未获实现授权。**
[详细迁移规格](platform-adapter-specification.md) C0–C7记录五站十三类闭包与主链，
[附录 C8](platform-adapter-symbol-ledger.md)逐项处置1234个定义，D记录接口和读取时点，G记录获准实施后的任务，
各任务卡的执行清单以 GitHub issues 跟踪。实施回归、安装、正式试跑与治理切换是独立门禁，本轮均未执行。

## 范围、基线与授权

- 首期必须五平台全部保持现有正常抓取能力。内部可分步，单路径验证不构成首期交付，
  未迁移平台保持原正式链路，不能逐站减损功能。
- 统一规则与结果，保留各站执行流程；只补明确缺口，不建平行框架。技术评审由实施方承担，
  外部选型不重开、新候选不安装。
- 前期已授权文档、保护性测试准备与隔离验证；v0.6/v0.7 仅修订本单、主设计、详细规格、C8附录、README导航及B站现状说明，
  不改实现、测试、配置或依赖，不解冻，不执行 Git 写操作，保留起始未提交编辑。
  用户已确认文档先行：P00-08 通过后仍先交付完整文档与执行清单，再单独授权进入 T00/P01。
  两个 issue 的实现与合并已单独获准并完成；平台 adapter 重构、真实采集或治理解冻仍未授权；
  即使 P00 准备就绪，也不能自动进入 P01。
- 当前分支 `chore/platform-adapter-preflight`；2026-09-27 原隔离测试的生产源码基线为
  `f2b0d3506e336bf15dbd1416f009bf8f3b09ac5f`，不是当前 HEAD；
  子模块 HEAD `2bcde689cc9a1b50cbcc7255597e9da1ff1a1b30`，已只读复核。
  原 v0.4 文档修订起点为 `9083585d724465b26007c90f90ffc1ca521874e3`。
  v0.5 起点为 `93f02ebfe8ccfc27f0e54748a60234eba8226583`；v0.6 本轮起点为
  `fe3e28ac7cc9575968e3279dd0e1c60ad0b7b1c1`，已将 main 合入准备分支；本次补审起始已有
  `docs/README.md`、本单、主设计三份跟踪文件修改及未跟踪的 `docs/platform-adapter-specification.md`，不是干净工作区；
  `main`/`origin/main` 均为 `b7e52db254530d2dcd7657a0560b0103e4ddb256`，是本次主线修复验收基准，不能与最初 f2b0 基线混称。
- 准备性测试已本地提交 `d1a0ce8f79842259a5569a5474774b352466dc8a`，中文标题为
  `test(采集): 补齐五平台字段与来源保护性基线`。原复评时点 `main` 和子模块未改，未 push、未 merge；
  新增 56 个保护用例仍仅在准备分支，不在当前 main。
- 原验证与独立评审日期：**2026-09-27（Asia/Shanghai）**；v0.5状态同步、v0.6详细规格及v0.7复核为 **2026-09-28**。
  v0.3 的 30 候选、17 组研究及全部历史失败记录保留。
  原准备未改业务源码、配置或依赖；其后两个 issue 修复已合主线，本轮不重写既有修复。
  历史版本操作约定仍见版本管理；它不授予本轮提交权限。
- 测试补丁内容标识（`tests/test_full_content_contract.py` 文件 SHA-256）：
  `ae2eb0d68a5db3cefdf14ca9f342af6c307bb5601528e419825d60fc2762c1e0`，与 candidate 副本相同。
  `git diff -- tests/test_full_content_contract.py` 的 SHA-256 为
  `297767b744992d37ba5107dd145aaca47e305cdf63df70b84ba44a9dc962ad77`；文件哈希与 diff 哈希不混称。

## 既有主线修复与验收证据（旧实现基线）

- [#2](https://github.com/ottercoconut/TripPostCollect/issues/2) 由 `e70843c` 修复，随 PR #3 合入 main，issue 已关闭。
- [#1](https://github.com/ottercoconut/TripPostCollect/issues/1) 由 `3e371f4` 等建立测试分组与 CI。
  首次 main `b2db8b7` 的 [36330351689 复验](https://github.com/ottercoconut/TripPostCollect/actions/runs/36330351689)
  有一项 macOS watchdog 时序失败，此历史保留。PR #4 的 `209f671` 改为有界 ready/release 与纯时钟保证 deadline，
  经独立 Codex 审阅无阻塞，已合至 main `b7e52db`，issue 已关闭。

合并后全新 [main 验收 36333548215](https://github.com/ottercoconut/TripPostCollect/actions/runs/36333548215)
使用托管 macOS **26.6.2**；Python **3.11、3.12 各自**均通过以下全部范围：

| 范围 | 每个版本 passed | 计数边界 |
|---|---:|---|
| component | 878 | main 根测试 |
| socket | 1 | main 根测试 |
| installation | 1 | main 根测试 |
| 真实 macOS OS | 51 | main 根测试；四 lane 合计 931 |
| fork 离线测试 | 417 | 32 文件，单列，不计入根 931 |

两版本所有 failed/errors/skipped/xfail/xpass 均为 0。验收时已实际下载产物，核对 OS 进程组退出、PF 恢复、
IP 拒绝与 socket 目录外拒绝。v0.5同步时只读取小摘要 `/tmp/trippostcollect-fixes.DfqzMt/main-summary.json`，
确认各组计数、`group_exited` 与 `policy_restored`；未重新下载或展开 CI 产物，其余探针结论沿用已提供的验收记录。

本机 macOS **26.5.2 / Python 3.12.13** 使用主线源码新副本及独立临时锁定 venv，
component 878、socket 1、installation 1 均通过。最初误用 Documents 内原 venv 被 Seatbelt 阻止 exec，
pytest 未启动；改用正确临时独立环境后通过，未放宽保护或改变原运行环境。
这是准备环境错误，不新增到历史 30 条失败节点，也不构成本机完整 OS 通过；真实 OS 证据来自上述托管环境。

准备分支另将 main 与原保护测试文件组成隔离副本，本机 component **934/934 passed**，
总收集 **987**、deselected **53**，其余 lane 此轮未重跑；计数为 main component 878 + 准备分支新增 56。
v0.5已只读核对 `/private/tmp/tpc-prep-component-DfqzMt/result.json`。934 不是 main 计数，也不是双版本结果。
临时摘要仅为证据索引，正文保留核心事实，不作为后续运行依赖。
当前验收证据另有本地持久副本 `.git/preflight-audit/main-b7e52db`，包含 review、main-summary、
完整两版本 main/pr4 脱敏产物及本机三 lane 计数；该副本不是运行依赖，也不是异地备份。

两个 issue 已解决、验证并合主线，不再是当前 P00 阻塞；v0.6另外补齐了静态迁移设计，不能把上述CI视为新包通过。
当前唯一可复用测试方式见[可复用测试运行](testing.md)，下文历史实验命令只作追溯。
本轮不运行测试/CI/安装；未来按详细规格F/G迁测试、分组及静态导入清单，不永久固定fork 417项或旧路径。

## 历史：原复评环境事实与验证缺口

以下为 2026-09-27 原复评时点的检测与缺口，不描述当前锁定临时环境；原复评未重测版本/包、不改两套环境；有限 OS 探针见失败复评：

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

## 本轮审计发现与 P00 准备任务

审计范围是上述 F/M 源码与起始文档工作树；定向阅读五站入口及可达模块，没有执行项目入口、平台import、
安装、JS或采集。先保留已有规格的主链设计，再补实际遗漏；不重开历史30候选研究，也不改已有测试结果。

| 缺口／矛盾 | 代码或文档事实 | 本轮处置与后续边界 |
|---|---|---|
| A01 完整闭包缺少统一检查面 | C2–C6已有主链，但无五站×十三类别总账；缺DY webid/短链、ZH枚举/错误/URL分流、XHS异常等落点 | 详规C0建立入口到环境矩阵，C2–C7补符号；不存在独立文件的项写明内联/共享/不适用 |
| A02 条件依赖与导入牵连混用 | M/tools/utils.py星号导入slider，cv2/numpy在旧worker导入期即触发；store/var/main还牵连DB、词云和未选平台 | C0分业务保留、旧导入牵连、退出切片；C7修正“条件cv2/numpy”；运行开关false不作为删除证明 |
| A03 共享核心闭包遗漏 | C/R/X导入failure_classifier，C/页面证据导入crawl_policy，页面证据导入ctf_browser_resilience | C1/C7和目标目录补唯一归属；失败裁决、冷却、页面证据流程不迁成平台私有能力 |
| A04 动态与资源链不完整 | E.runpy/局部hook、行为桥八个局部import、cache memory/redis、xhshow延迟加载、execjs编译时点不同 | C0/C7与D3分开登记；保存三JS及LICENSE散列；B站仍依赖fork检查/profile/stealth，即使无fork Python import也不能先删树 |
| A05 B站文档与代码时间/图片语义有差异 | 起始platforms/bilibili.md:38称详情时间优先；C.hydrate_bilibili_article_record:4861保留搜索时间。原文:33–34的图片“优先”实际为C:4531合并Opus→HTML→content_pic_list，仅无图才fallback | 本轮P00-07已定向修正文案；首次机械迁移按C5保留现状，不在adapter设计里另造时间规则 |
| A06 登录和资源容错易被统一抹掉 | B站正式Cookie、warmup的SESSDATA或DedeUserID、repair的SESSDATA+nav.isLogin不同；C对缺失stealth原本跳过 | C5补三种谓词及缺资源分支；资源必须随包交付与运行期错误等价分别验，不用安装要求暗改现行容错 |
| A07 就绪/授权/工作区表述过期 | 顶部、结尾及详规H多次称无已知阻塞；“本轮起始干净”及本轮提交暗示与当前事实/指令冲突 | 四份设计导航统一到本单状态；保留历史结果，删除本轮提交暗示 |
| A08 重复定义和编号歧义 | 主稿S06历史示意类型、S15历史接缝、S25研究T01–T17与详规G实施T00–T14容易混读 | C/D/G为唯一详细定义；主稿保留历史并显式区分研究编号；本单P00-01–08只管准备产物 |
| A09 执行器与共用fork符号大量无去向 | C的135个顶层定义中约100个未在C0–C7出现（Cookie族、run_command监督族、摘要、身份投影、正式图片、db事务、runtime_blocker等）；fork共用工具、四站私有方法、XHS core约50个方法只按行段覆盖 | 新增附录C8：AST枚举92文件1234定义，全部有目标/处置/卡/测；生成器遇未映射定义即失败 |
| A10 活跃符号漏列 | `should_reseed_douyin_frontier`（DY core:611调用）、微博`stagnation_basis`、`env_int`、image_manifest四个稳定键与upsert、图片错误分类重复实现 | C1/C0补行，D4补`ImageStager`端口与`should_reseed_frontier`方法；C8/X2–X6、X10 |
| A11 退出切片仍被保留代码引用 | 73个“退”符号在评论/代理/creator/基类/工厂/词云分支被引用；只删文件会导入失败 | C8列出全部调用点与切断方式；T12卡负责按表切断 |
| A12 锚点漂移与反向导入 | ZH field/exception、registry、scheduler/discovery、WBI常量、两条S18测试行号偏移；B修复与W从C反向导入 | 锚点已按F修正；反向导入列入C1/X8，作为T08/T10删除前置 |

以下为**P00文档准备任务**，与详规G未来实现卡不同。状态“已补”只证明本轮静态文档产物完成；
旧节点台账的运行收集、import trace、仓库外安装、故障注入仍留实施阶段，不要求运行尚未实现的新包。

| ID／前置 | 可执行动作与固定输入 | 产物、验收和当前状态 |
|---|---|---|
| P00-01／无 | 核对HEAD/fork、起始git status与文档职责；按本轮指令清理提交/实现暗示 | 本单基线与A07；保留全部起始编辑，只改五份文档。**已补** |
| P00-02／01 | 从R/C/X/E/main逐站追到十三类别，逐项标内联、共享、牵连或退出；补短链/webid/枚举/异常/缓存基类 | 详规C0与C2–C7；五站十三项无空白，存活能力有唯一目标与实施测试责任。**已补** |
| P00-03／02 | 对main/config/utils/store/var/proxy/cache和八个行为桥分别列真实导入边、条件与最后删除前置 | 详规C0/C7；包含局部import、runpy、sys.path及模块编译，区分“未调用”和“未导入”。**已补**；动态可达性由T12验 |
| P00-04／02 | 对照C/R/X/repair/warmup/页面证据的根依赖，逐个指定项目核心与本站边界 | 详规B/C1/C7；policy/failures/browser_runtime/page_readiness有归属，平台无调度/冻结/净化算法/事务/ACK裁决权。**已补** |
| P00-05／02、03 | 将每站配置文件、父覆盖、操作时读取与资源需求对应D1–D3；核对JS/许可字节和fork路径 | 详规C0资源散列、D读点；列Python包/Node/浏览器/UI/可写目录，XHS无签名JS但有条件stealth；不输出秘密。**已补** |
| P00-06／02–05 | 将本轮新增边映射到现有F测试责任与T实施卡，并登记退出切片剩余引用的检查范围 | 详规C/F/G；每个补项有检查责任，不用原931/417计数替代新覆盖，不把T00当作首次建立闭包。**已补** |
| P00-07／05 | 针对A05逐句核对B站平台文档与normalize/hydrate/extract函数、现有article测试；形成仅文案差异补丁及复核结论 | **已补**：`docs/platforms/bilibili.md` 两条说明与当前函数一致，保留平台原始时间要求；既有测试只静态阅读，不声称本轮运行通过，无行为改动 |
| P00-08／01–07 | 对文档diff、C0十三类别、C1–C7归属、D读取、F/G前置和链接做复核；逐条关闭A01–A12或留下具体阻塞 | **已完成（2026-09-28，基线F/M/U↑）**：程序化核对243个符号锚点、7个入口CLI默认值、env名称双向对照、测试文件与节点、1234定义全量处置；A01–A12全部关闭，结论见文末。不是安装/运行通过；实施仍需另获授权 |

P00退出标准是准备产物准确、差异处置清楚且独立复核结论可追溯；不是“写了完整清单”或“旧CI绿了”。
本轮交付包括已补项与明确待办，不自动关闭整个P00，不进入P01。

## 五平台静态设计索引（v0.6起）

以下路径以仓库根为基准；`M/` 表示 `tools/MediaCrawler/`，`C` 表示
`scripts/mediacrawler_crawl.py`，`E` 表示 `scripts/mediacrawler_export_entrypoint.py`。
platform、xhs、boundary三份专项报告已读取并提取入详细规格；源码锚点固定本轮SHA，目标路径均标未实现。
临时报告不作为后续实施依赖；报告中的过程自述不提升为项目规则。以下是当前结论，不补签动态验收。

| 平台 | 正式入口、执行与登录 | 请求、JS 与导出接缝 | 规格位置／未来验证 |
|---|---|---|---|
| B站 | 通用runner→C自有article；仍执行器原进程 | 行为Context关闭后沿urllib/WBI/本轮Cookie请求；详情→图→粉丝→净化，轮末JSONL；不替换fork视频 | 详规C5/D3已定；T08验字段/顺序/图片/辅助修复，T12验stealth与独立安装 |
| 微博 | 通用runner→worker，原core/login独立流程 | HTTPX；长文详情直连一次与搜索重试分开；repair才启同Page fallback；无签名JS但有stealth | C2已定；T05验长文/过滤空/图片pid/登录/序列化 |
| 抖音 | 通用runner→worker，原浏览器response监听/分页 | douyin.js/execjs保留；page/offset/search ID成组；phone MEMORY/slider分支仍活跃；repair同Page fallback | C3已定；T06验空首屏/签名body/条件登录，原来源标记差异另案 |
| 知乎 | 通用runner→worker，正式CfT/CDP | HTTPX与execjs+zhihu.js；导航后行为前刷新Cookie；Parsel精确entity；原404/请求失败/解析失败分类 | C4已定；T07验answer/article/图片/签名；辅助preferred_engine不改变正式driver |
| 小红书 | 独立root→C→worker；每轮空profile扫码，一次Chrome/单Context | xhshow原调用级生命周期；平台session/login/detail/media拆分；根lease/terminal/discovery保留；worker显式事件后publish | C6/E2已定；T09验登录/关闭/ACK窗口；ACK不等ContentCommit，合法旧ACK不回滚 |

四站共同导出已定位 E `install_export_hook`：包装 `AsyncFileWriter` 的 CSV、JSONL、单条 JSON 方法，
首次项目序列化前调用 `sanitize_export_item`。C 构造正式 worker 时选择 `--save_data_option jsonl`，
四站 store 的 JSONL 实现分别位于 `M/store/<platform>/_store_impl.py`；`main.py` 还有结束 flush/关闭分派。
v0.6已将异常出口、manifest、控制事件、日志和关闭链迁移归属列入详规C/E；动态等价仍需实施验证。
store投影/下载/JSONL存活切片保留，registry/DB/GUI牵连只有T12/T14引用与安装门禁满足才能删除。

## 公共接口与状态、进程归属

完整目标签名和输入类型以详规D4为准；此表只索引责任，不留第二套示意API。应用规则可在worker内执行，不反向import scripts。

| 接缝 | 当前所有者及边界 | 已定设计与后续验收 |
|---|---|---|
| CLI/env、选站与启动 | 通用 runner 管调度，XHS root 管槽位/租约；C 构造四站 worker 命令，B站原进程执行 | 详规D1/D2定默认/覆盖/FP/敏感项；唯一-m入口platforms.entry，入口与core.paths登记例外 |
| 平台记录 → 字段裁决 | C `validate_formal_record`；`published_at_for_record`、`row_for_record` 做时间/行投影 | 本轮保护测试覆盖五站 count/source/observed、时间与正文状态；投影 `status` 本身不是正式成功裁决 |
| 平台分页 → 控制事件 | 各站原 worker/core 解释游标；application 决定候选/停止与推进资格 | 详规E3分legacy吞错、XHS重读强校验、严格冻结状态；不全改fail-closed |
| 内容与媒体提交 | C `materialize_formal_record_images`、`import_valid_records_with_media_rollback`；数据库/文件层完成事务动作 | V08 待验证；不把 worker JSONL 写出当 SQLite 内容提交 |
| 通用发现记忆 | C `persist_discovery_checkpoint`，通用任务查询指纹隔离 | D3确定启动一次集合与runner/提交重读；FP保留六项排除，params.behavior_profile参与FP |
| XHS 批次发现记忆 | child `publish_batch`，root `BatchCheckpointCommitter` 与 terminalizer 掌握提交/ACK/中断线性化 | V07/V11 待验证；内容提交与已 ACK 发现提交不混淆，子进程不取得长期数据库提交权 |
| 浏览器、登录与清理 | 各站原 runtime/worker 负责所持资源；XHS root 监督唯一轮次和租约 | 保留非XHS已有CDP fallback，不新增重启；HTTP客户端数量不等于Context数量；原嵌套重试不改 |
| 最终完成 | 现有执行器门禁与 runner 状态写入；路径引用 `trippostcollect.core.paths` | 只引用正式契约，不由本设计增设成功条件或手工修正执行状态 |

## 内部任务、验收与回退

| 单元 | 可执行的工作与当前进度 | 验收证据 / 退出门禁 | 回退边界 |
|---|---|---|---|
| P00-A 文档对齐 | v0.7主稿/P00/详细规格/C8/导航一致；历史研究与实验分区保留 | 文档diff、现有链接、事实符号、冻结校验；不补签旧结果 | 按补丁逐段撤回，保留v0.3研究 |
| P00-B 保护性测试 | 在原 `formal_records` 与参数化上补强；两解释器各 66 例通过 | 批准保护性测试准备；保留失败，不改业务代码、不 skip/xfail 掩盖 | 仅撤回新增测试/参数集合补丁，不删除原断言或 fixture |
| P00-C 详细规格 | C0闭包总账、C1–C7补项及C8全量符号账，P00-01–08已完成 | 详规C–G；静态准备与未来动态安装/故障验证分开 | 仅修正文档，当前不删树或切入口 |
| P00-D 隔离验证 | 原 29/30 红项复评及失败保留；当前主线双版本根 931、fork 417 各自全过；准备分支本机 component 934 通过 | 见当前验收证据；不同版本、基线和 lane 不混计；未来边界变更仍须适用验证 | 丢弃隔离临时产物，不接触生产状态或环境 |
| P00-E 技术评审 | 三份专项只读核对与P00-08程序化复核均已纳入 | 未获实现授权；terminal.finish诊断风险及行为强化另案，见详规H | 保留现有五站实现，禁止自行进入P01 |
| P01—P06 后续实施 | 详规G的T00–T14给出文件符号、前置、禁止项、检查/产物、回退/删除条件 | 实施另获授权；五站完整I门禁后再过正式L门禁，不能单站交付 | 按接缝回退；不回滚业务数据、不删除已ACK快照、不手改checkpoint |

v0.6/v0.7文档级核验：已对规格的显式源码符号/行锚点做静态AST核对，并生成C8全量账，检查现有相对链接及签名块语法，
并确认主稿S23–S25研究内容未改变；文档diff无空白错误。
激活`.venv`后执行`python scripts/verify_frozen_files.py`通过，冻结正文/登记未变。
未运行测试、CI、项目入口、JS、安装或线上验收；冻结校验仅执行其既有路径导入，不作平台import验证。

## 历史：契约测试审查与验证结果

以下至“原复评审计附件”保存v0.4/v0.5的原实验与审计事实；其中当时未就绪/未运行等判断只属于该时点。
v0.6不重跑这些实验，不把个人临时路径的历史重建命令作为公共现行测试入口。

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

2026-09-27 已执行结果如下。原独立评审读取 XML 复算计数与哈希，并比较失败节点集合，
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
本节直到历史审计附件均为 2026-09-27 原复评记录；“未运行/未修改/未授权”及风险判断按该时点理解。
后续修复与全新验收见上文，不能把旧诊断直接当作当前 bug 或 30 项未决清单。
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
原复评时P00未就绪，原因是当时五站闭包及精确接口、读取时点未齐；v0.6状态见顶部，完成准备仍不等于实施获准。
OS/安全清理证据按受影响边界安排，不把所有整改前置到当前文档准备，也不把它们推迟到正式切换之后。

#### 测试必须随模块重构

冻结的是业务断言和保护语义，不冻结测试文件、私有函数、旧脚本名或 monkeypatch 架构。
原复评的 30 个失败节点未发现已废弃、可直接删除的保护语义；后续可移动、拆分、重写 fixture 与调用入口。
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

以下保留两项原诊断的因果与风险记录，分别说明修复边界。
`leases.py:LeaseGuard.__exit__/close` 的清理 RuntimeError 可覆盖原业务异常；此覆盖问题已由 #2 修复并合主线，
不再列为当前未修问题。原诊断要求专门回归“原异常 + 清理异常”的优先级与可观察诊断，不能据此定案为正常抓取业务 bug。
`terminal.py:XhsRunTerminalizer.finish` 捕获清理异常后，返回对象与磁盘 provisional/pending 摘要可能不同；
此诊断持久化风险未包含在 #2 修复中，也未在本次修复中改动或验收，作为独立风险保留，不能随 #2 宣称消失；
它不是本次两个 issue 的关闭条件。
需专门异常路径回归返回值、落盘摘要/状态与租约保留的一致性，不能混同为没有记录中断。
相关行为如需改变，应在对应清理/终态边界变更时另评并验证，不以文档复评授权顺手修复。

初次试跑为 561 通过、8 失败并提前停止，不是最终基线，不与上表重复累计。其中 1 个 pytest 测试
报告 4 个副本文件缺少 uchg（git archive 不保留该标志），不是 4 个测试失败；
随后仅在隔离副本四个已登记文件恢复 uchg 元数据（metadata）。
真实原件及登记哈希未改、未解冻。允许记录并恢复副本元数据，不能把这项操作误写成禁止事项。
最终全量结果不再含该项副本冻结标志检查失败；该测试审计当时未另跑冻结验证。
本轮文档修订的冻结校验单列，不改写上述测试结果，后续提交仍须执行项目要求的验证。

### 历史隔离与可重建步骤

以下仅保存原实验过程，不应直接复用；当前测试统一遵循 [可复用测试运行](testing.md)。

基线和 candidate 均由上述执行测试时的生产源码基线与子模块 HEAD 的 git archive 构建，只有 candidate 复制测试补丁。
未复制生产 data、outputs、凭证、profile 或 venv，也未新装解释器或包；测试只读取现有 venv。
env -i 清空继承环境，HOME/TMPDIR/PYTHONPATH/TRIPPOST_PROJECT_ROOT 均指向临时区。
sandbox-exec 禁 network*、真实仓库写、生产数据与凭证/profile 读取以及浏览器执行。

下列为原时点复建说明，**本轮不执行，也不作为现行标准**。原实验在项目根目录，用已审阅且哈希相符的测试文件构建副本：

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

### 原复评审计附件

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

## 当前设计结论与复核门禁

原独立评审批准文档对齐、保护性测试准备及当时隔离验证的证据记录；v0.5同步#1/#2已验证、关闭并合主线。
新增测试实质检验现有契约，没有弱化原断言。测试准备的通过不构成零缺陷结论。

P00-08 复核（2026-09-28）逐项结论：

| 复核对象 | 方法 | 结论 |
|---|---|---|
| 符号行锚点 | 按缩写解析到固定F/M文件，逐个核对定义行 | 179处准确；漂移项（ZH field/exception、registry、scheduler/discovery、WBI常量、S18两条测试行号）已修正 |
| D2 CLI | AST提取7个入口全部`add_argument`的默认值、类型、choices | 与D2逐项一致 |
| D2 env | 源码中全部`TRIPPOSTCOLLECT_*`名称与D2展开结果双向对照 | 无遗漏；D2中“父发无消费”项与源码一致 |
| 测试引用 | F/S18引用的测试文件、`::`节点与行号 | 现有文件与节点均存在；缺失项均为计划新建文件或外部参考链接 |
| 符号闭包 | AST枚举C8范围内1234个定义，逐项匹配处置规则 | 全部有处置；退出符号的73处保留代码引用已列切断表 |
| 上游对照 | 本地`../MediaCrawler-upstream`与fork分叉点逐文件比较 | 本地改造与原样文件已分类；上游媒体下载重构不采用；抖音Argus头列R06 |

A01–A12 全部关闭。D/E继续唯一管理CLI/env/wire/指纹、端口、进程、读取与错误；F/G管理获准实施后的测试、构建、删除与回退。
29/30红项是历史，不作为当前未决bug列表。V07/V08/V11 等故障窗口不能由纯函数测试代替，按对应阶段验收。

另案风险独立保留：terminal.finish诊断持久化未由#2解决；legacy事件严格化、原来源/缺值、抖音风控头等差异不得藏进机械迁移。
未来实施必须五站完整回归、仓库外安装、静态/动态引用检查及CI测试职责对账；正式切换另需授权及旧轮安全处置。
首期五站全功能不退化，既有wire/runtime/知乎签名、XHS ACK先于最终内容提交均保持。
**P00 完成；仅修改文档，未提交Git，未进入实现、采集或治理解冻。进入 T00 须另获实现授权。**
