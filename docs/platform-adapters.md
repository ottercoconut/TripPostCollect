# 平台实现解耦设计：讨论稿 v0.5，未批准实施

> 状态：目标设计，供讨论与评审；不是实施授权、迁移完成报告或现行操作手册。
> 前期已获准文档、保护性测试及隔离验证；本轮仅修订本文与 P00 工作单，不进入实现，不改变正式路径、数据、测试、配置、依赖、AGENTS 或冻结资产。
> 本文的“必须”约束未来迁移方案；现行运行仍以权威文档和当前实现为准。

## S01｜目标、基线与证据边界

现有五站可用。目标是在保持可用性、行为和证据的前提下，解除对上游运行树、配置体系、
目录布局及持续合并流程的依赖，由用户承担后续维护。科研用途、无商用，不改变采集范围。
本次不借搬迁重写签名、浏览器驱动、请求发送方式、分页算法或正式完成规则。
v0.3 在原 v0.2 上局部修订，增加 2026-09-27 的五站与相似工程复用研究（S23—S27）；v0.4/v0.5 保留全部 30 候选、17 组研究。
研究确认范围、来源等级和选型建议，不表示已安装依赖、验证端点或批准迁移。
用户最新确认：首期交付必须五平台全部保持现有正常抓取能力，不能以单平台试点交付代替。
内部可以分步迁移，但不得逐站减损正式五站功能。助手承担技术评审，新增构件只补明确缺口，
不建平行框架；外部选型本轮不重开、不安装新候选。平台 adapter 重构、真实采集与治理解冻均尚未授权。
用户另行授权的 #1/#2 修复已验证、关闭并合主线；v0.5 仅同步这些既有结果，不重写修复，不自动宣布 P00 ready。
当前准备进度、隔离验证结果与独立评审集中于 [P00 工作单](platform-adapter-preflight.md)。
本文 B03、S18、S23—S27 的既有审计及“本轮”观察均指 v0.2/v0.3 研究时点，不能代替当前验证记录。

本稿原设计基于 B01/B02；文中历史源码行号用于定位该版本，不保证未来移动后仍有效。当前修复验收另列 B05/B06。

| 编号 | 基线或证据 | 使用边界 |
|---|---|---|
| B01 | 根 HEAD `f2b0d3506e336bf15dbd1416f009bf8f3b09ac5f` | 起草时已核对 |
| B02 | 子模块 HEAD `2bcde689cc9a1b50cbcc7255597e9da1ff1a1b30` | 起草时已核对 |
| B03 | `/tmp/trippostcollect-decoupling.lfAPem/audit.md` | v0.2 起草时只读审计；非全量依赖审计 |
| B04 | 本稿所列精确源码与权威文档章节 | v0.2/v0.3 研究时仅定向静态读取，未执行测试或平台请求；2026-09-27 隔离测试结果见 P00，不包含平台请求验收 |
| B05 | main/origin main `b7e52db254530d2dcd7657a0560b0103e4ddb256` | 当前主线修复验收基准；#2 的 `e70843c` 随 PR #3 合入；#1 的 `3e371f4` 等与 PR #4 `209f671` 已合入，不替换 B01 历史基线 |
| B06 | 准备分支 `chore/platform-adapter-preflight` @ `93f02ebfe8ccfc27f0e54748a60234eba8226583` | 本轮文档起点，已将 main 合入准备分支；fork 仍为 B02；额外 56 个保护用例仅在准备分支 |

B03 是 v0.2 起草时的外部审阅输入，不作为正式流程必须存在的文件；其关键证据已列入 S15、S18。
“已定位”表示找到静态调用或测试函数，不表示覆盖完整、测试通过或线上行为已重验。
未重新核验源文件总行数、配置读取次数及依赖占比；这些数字不作为迁移依据。当前测试数量以 P00 分基线、版本和 lane 的验收记录为准。
动态导入、异常支路、安装资源和可选依赖的完整闭包仍需 S16、S17 所述工程核验。

## S02｜权威来源与不可变范围

本文只描述目标边界、迁移方法和验收要求，不复制另一套现行规范。
发生描述冲突时，不由本文另创业务语义；应记录差异，核对具体实现和测试，再按 S20 治理。

| 唯一真源 | 负责内容 | 本文如何引用 |
|---|---|---|
| [正式契约](formal-crawl-contract.md) | 完成、失败、字段门禁、发现记忆和冻结状态 | application 的裁决依据 |
| [架构](crawl-architecture.md) | 现行组件、进程和数据流 | S03 是迁移接缝说明 |
| [持久化](data-persistence.md) | schema、文件归属、事务与入库校验 | S11—S12 只划分目标职责 |
| [运行手册](operations-runbook.md) | 正式命令、人工操作和恢复 | 不在本文发布替代运行命令 |
| [字段覆盖](platform-field-coverage.md)、[数据字典](crawl-result-data-dictionary.md) | 字段能力与研究查询含义 | 不随类型拆分重定义字段 |
| [平台文档](README.md#按任务阅读) | 各站登录、游标、响应与特有失败 | 平台例外不提升为五站通则 |

青岛主题范围、只采图文、作者字段、头像清理、发布时间来源、正文图片门禁均保持现状。
`web_posts` 与 `ctf_captures` 分工不变；独立 TripPostAdmin 仍只通过既有数据与文件契约协作。
不新增城市文本判断门禁，不恢复废弃参数，不把诊断、详情修复或页面证据入口变成正式搜索入口。
正式成功仍需契约定义的耗尽证据及全部门禁；数量、页数、停滞不能替代来源耗尽。
结构有效但 `topic_relevant=false` 的帖子仍进入正式内容集合；相关性只用于统计与下游筛选，
不作来源耗尽或入库硬门禁。

## S03｜现状拓扑与目标部署边界

```text
通用 crawl_runner                         XHS xhs_runner
调度、冻结、通用任务监督                   槽位租约、临时 session、根监督、批次提交
       │                                          │
       └────────── mediacrawler_crawl 执行器 ──────┘
                    ├─ B站 article：执行器自有搜索/详情/作者/图片分支
                    └─ 微博/抖音/知乎/XHS：export_entrypoint 平台 worker
                         └─ 当前上游 main → 平台 core/client/login/store
```

通用 runner、执行器、平台 worker 是不同角色，不统称同一个 crawler。
通用命令构造和启动见 `scripts/crawl_runner.py:352、851`；执行器创建 worker 见 S15 的 M02。
XHS 的独立入口不并入通用调度；通用平台并发及同平台串行规则仍引用正式契约。

B站在执行器中提前进入 `run_bilibili_article_search`，绕过上游 B站 core/client。
其迁移对象是根脚本中的 article 函数及真实依赖，不能用上游视频或动态代码替代。
迁移后 B站仍在执行器进程内调用平台包；不为了目录统一新增 B站 worker。
其行为浏览器与自有 HTTP 链路仍保留原请求方式，不能推导为所有请求都经浏览器发送。
B站行为 Context 关闭后才沿既有 HTTP 链路请求；“当前会话”不泛指各站全流程共享一个仍存活的
浏览器 Context。XHS 全轮单 Context 的限定保持不变。

其余四站保持原平台 worker 进程形态；用新的包入口替代内层 runpy 桥，见 S14。
application 是逻辑层，可以分别存在于执行器、worker 和 XHS 根监督进程。
子侧继续执行分页、候选筛选和补详情；父侧掌握正式提交及最终完成裁决。
平台分页仍在原执行位置迭代，不新增五站逐页 RPC，也不把 XHS ACK 推广成通用分页协议。
跨进程共享纯数据与既有证据协议，不共享进程内会话对象或可变配置单例。

## S04｜目标逻辑模块与职责

下表是逻辑职责，不要求建立同名顶层目录或全套新框架。
优先复用 `core`、`platforms`、`records`、`artifacts`、`db`、`scheduler`、`xhs` 等现有包。
新文件只在职责确实独立时出现；类名、协议名和最终目录在实现评审中确定。

| 逻辑边界 | 应承担 | 禁止承担 |
|---|---|---|
| contracts | 必需输入/观测/决策数据、窄接口、稳定枚举 | 实现完成裁决、启动浏览器、读取数据库 |
| application | 按正式契约编排、批准推进、选择失败分流、执行门禁 | 解释站点签名和游标字符串、直接实现 SQL/HTTP |
| platforms | 站点请求、解析、登录/签名细节、游标编解读、鉴权下载 | 自定正式成功、跨平台导入、直接提交长期数据库 |
| runtime | 原进程树监督、资源生命周期、计时与等待机制 | 增加隐式重启、扩大重试、决定内容有效性 |
| records | 记录投影、来源信息、强制清理、旧协议 serializer | 发请求、用缺失值伪造真实空值、另立完成谓词 |
| artifacts | staging 证据校验、字节复验、路径安全、去重与晋升 | 裸 URL 补图、替代平台鉴权、决定发现 scope |
| db | 内容及发现记忆的窄读写、SQLite 事务实现 | 控制翻页、管理浏览器、凭日志自行提交 |
| bootstrap | 解析边界输入、选择平台、构造实现和注入依赖 | 把所有业务集中成新万能入口 |
| scripts 薄包装 | 保留获准保留的外部命令并单向转发 | 被内部模块反向导入、继续承载业务 hook |

application 定义调用顺序和决策；artifacts/db 实现其请求的技术动作。
内容提交的 application 所有者仍在执行器；XHS 批次发现提交所有者仍在根 runner。
平台可以提供平台特有能力，不能为了单一 ABC 强迫每站实现不适用的登录或分页方法。
runtime 只抽出确需隔离的启动、借用、等待和清理能力，不重新包装全部 Playwright API。
用户已确认：统一规则与结果，保留平台特有执行流程，不强迫五站统一完整抓取循环。
解耦以职责、权限和状态所有权为界；共同规则可复用，各站登录、请求、分页与详情编排可以不同。
用户同时确认优先复用成熟构件，不自行重写已有可靠基础能力；具体依赖与内部迁移顺序由助手技术评审，
不重复让用户选择试点或审查技术细节；此项职责不授予实施权限。

## S05｜依赖方向矩阵

符号：`直`允许直接模块导入；`口`仅经 contracts 窄接口调用；`—`禁止；`本`为本层内部。
此表区分直接模块导入与通过 contracts 接口调用；运行时调用方向不代表对具体实现的反向 import，
接口与实现由 bootstrap 连接。
此矩阵约束目标态；现有反向导入属于待迁出接缝，不能被误写成已消除。

| 调用方 → 被依赖方 | contracts | application | platforms | runtime | records | artifacts | db | bootstrap/scripts |
|---|---|---|---|---|---|---|---|---|
| contracts | 本 | — | — | — | — | — | — | — |
| application | 直 | 本 | 口 | 口 | 直 | 口 | 口 | — |
| platforms | 直 | 口⁵ | 本¹ | 口 | 直 | 口 | 口² | — |
| runtime | 直 | — | — | 本 | 直³ | — | — | — |
| records | 直 | — | — | — | 本 | — | — | — |
| artifacts | 直 | — | — | — | 直 | 本 | 口⁴ | — |
| db | 直 | — | — | — | 直 | — | 本 | — |
| bootstrap | 直 | 直 | 直 | 直 | 直 | 直 | 直 | 本 |
| scripts | — | — | — | — | — | — | — | 直→bootstrap |

¹ 平台互不导入；共用工具放共享边界，不用一个平台充当另一个平台的工具库。
² 仅已知候选、查询等必要只读接口；不允许平台取得任意连接或 SQL 写入口。
³ 仅使用清理后的日志/证据表示，不把完整 transport 对象传入日志。
⁴ 仅既有引用核验等必要窄接口，跨文件和数据库操作仍由 application 编排。
⁵ 仅当前子侧纯决策/预算等窄端口；不直接 import application 实现，不授予平台正式数据库提交权，
不递归触发新平台采集。
bootstrap 可以引用实现但必须按 selected platform 延迟加载，未选平台不触发签名/浏览器初始化。
平台循环调用注入的决策端口时，端口定义在 contracts，实现可在 application，不构成反向导入。
`core.paths` 继续提供路径定义；“共享底座”不是规避上述依赖方向的业务聚合模块。
矩阵约束项目模块的职责关系，不禁止基础库。需隔离的是外部平台 SDK 模型、HTTP Response、
Selector、第三方异常和全局状态；项目自己拥有 schema 的 Pydantic 模型可实现 contracts。
Facade 只设在变化、权限或状态边界，不为每个标准库函数、校验器或 HTTP 调用再造一层框架。

## S06｜数据类型与现有 wire 协议

以下名称均为示意类型，不宣称代码已存在；不新增 JSONL、manifest、event schema 版本。
内部类型允许改变，外部字段名、字段省略规则、事件顺序和路径由兼容 serializer 保持。
dataclass、TypedDict 或项目自有 Pydantic 模型均可作为实现选择，不以“纯净”要求排除成熟库。
外部 SDK 的模型与默认补值不能直接变成本项目契约；Pydantic 类型校验不证明平台来源真实。
HTTP Response、Selector 及第三方异常在各自边界内消费，再映射为项目观测或现行错误分类；
项目自有模型可跨项目逻辑层使用，但不能把 Cookie、transport 或任意对象藏进模型后序列化。

| 示意类型 | 必需表达的信息 | 不应混入 |
|---|---|---|
| `SearchRequest` | 查询来源参数、请求位置、refresh/deep 阶段、作用域引用 | 最终成功标志、浏览器实例 |
| `PageObservation` | 响应状态、候选、是否观察到 has_more、下一位置、来源证据 | 已批准写库前沿 |
| `DetailObservation` / `AuthorObservation` | 值、来源、观测状态及具体错误 | 用空字符串或 0 掩盖未观察 |
| `ImageObservation` | 权威图片候选、下载结果、manifest/staging 引用 | 可直接晋升的无校验路径 |
| `CandidateDecision` / `AdvanceDecision` | 应用层继续/跳过/失败及推进批准 | 平台自行生成的正式完成结论 |
| `ContentCommitResult` / `DiscoveryCommitResult` | 各自提交结果、引用和提交是否确定 | 将两个提交合为单一 success |

观测结果不是可直接入库的大字典：先经 records 投影及来源校验，再走应用门禁和 artifacts/db。
“未观察”“解析失败”“已观察为空”“已观察为 0”必须可区分；状态与值不能互相代替。
粉丝数 0 只有同时存在合格来源和 `followers_observed=true` 才表达真实观测。
平台不提供的字段仍只通过现行配置声明 ignored，不由 parser 自动把失败转成 ignored。
发布时间保留平台原始来源，转换逻辑沿用现行规则，不能以本轮时间补缺。

共享旧协议 serializer 负责内部类型到当前 wire shape 的映射，包括失败信息的允许表示。
不同平台字段不强制压成无类型扩展字典；必要扩展应有清晰来源和边界。
Cookie、签名计算上下文、Page、BrowserContext、browser 实例不进入新增 IPC 数据类型。
既有必要登录输入通道保持现状；不借类型迁移扩大秘密持久化或把内存会话复制到批次快照。

## S07｜分页、scope 与已知候选读取

平台解释 cursor，application 持有 scope 并批准推进，db 只落实获准的持久状态变更。
核心不拆 cursor 字符串、不根据字符串格式猜下一页；可携带平台定义的位置对象与原 wire 值。
平台返回“观测到的下一位置”不等于已提交恢复位置；完整性及安全边界需经现行门禁。

| 平台差异 | 保持要求 | 权威说明 |
|---|---|---|
| B站 article | 原搜索页、详情补全与已知 ID 提前跳过 | [B站](platforms/bilibili.md) |
| 抖音 | page/offset/search ID 成组；空首页、建链和重建链条件保留 | [抖音游标](platforms/douyin.md#搜索响应与游标) |
| XHS | 保存 client search ID；账号隔离；批次 ACK 独立处理 | [XHS](platforms/xhs.md#批次恢复点) |
| 微博、知乎 | 各自分页和权威详情来源保持 | [微博](platforms/weibo.md)、[知乎](platforms/zhihu.md) |

refresh 不推进 deep 前沿；抖音正式文档允许的重建链有独立证据和条件，不能被通用抽象抹掉。
首次从第一页、有限顶部刷新后恢复深层、耗尽后的刷新规则均直接引用正式契约。
完整批次才允许记忆已处理候选；未完整尾批不得将候选标 seen 或跨过未持久化部分。
视频跳过、决定性字段无效、候选级失败和结构有效记录保持不同决策与计数含义。

当前 adaptive 存在直接读取 SQLite 内容与候选记忆的路径，不能假定只是启动时导入一组 ID。
目标经 `KnownCandidatesReader` 等窄只读端口隔离，端口名称是示意，不新增缓存系统。
迁移前逐调用记录读取时机、事务边界、当前轮增量、并发写入可见性及排除表读取行为。
未核验前保持原查询时点与刷新方式；不得擅自改成每轮不可变快照或每候选重查。
通用 job/query scope 与 XHS target/account/query scope 不合并，历史累计与当前 child 集合也不混淆。

## S08｜配置、环境与持久状态

外部继续使用既有 CLI/env；bootstrap 在每个现有进程入口解析并注入显式对象。
配置拆分依据生命周期和使用者，不把所有历史全局值塞入一个万能 Config。

| 类别 | 典型内容 | 生命周期与所有者 |
|---|---|---|
| 查询来源参数 | 平台、关键词、影响搜索结果的筛选项 | 冻结计划；指纹使用既有来源投影 |
| 运行策略 | 等待、有限重试、运行预算、字段 profile、行为策略 | application 持有，按需给能力端口 |
| 平台/浏览器配置 | Chrome 路径、平台选项、现有驱动选择、资源位置 | bootstrap 解析，runtime/platforms 消费 |
| 内存会话 | Cookie、client、签名上下文、浏览器对象 | 当前拥有者进程；不混入普通配置输出 |
| 持久状态 | checkpoint、seen、累计摘要、租约、execution state | 项目持久化接口；不是配置默认值 |

内部代码最终不随处读取 `os.environ` 或全局 `config`；入口间已有 env wire 不等于全局读可保留。
初期兼容适配可保留原读取行为，但每个调用点必须登记注入替代项与退出阶段，见 S17。
读取时机也属行为：导入时求值、CLI 赋值后读取、轮内变化不能在机械迁移中悄悄互换。
默认值、缺值错误、显式 false/0、平台未使用字段的处理均需旧新差分。

`query_fingerprint` 保持原算法、规范化和序列化，不能散列新配置对象的所有字段。
超时、登录、顶部刷新预算等非来源参数不应因对象扩展意外进入指纹。
既有查询来源参数变化仍形成新 scope；不为“兼容”把真正不同查询归并到旧记忆。
所有路径继续由 `trippostcollect.core.paths` 等现有路径能力提供，不靠源码 cwd 拼接。

## S09｜资源所有权、登录与重试预算

Chrome for Testing、既有驱动及原请求发送方式已确定保留；正式链上的 Playwright 按原位置使用。
Patchright/Scrapling 是既有页面证据等辅助路径，不推广为五站正式链；B站 article 的 urllib 不换 HTTPX。
知乎 `execjs + JS` 没有本次需解决的痛点，保留桥接、JS 与运行时，不转浏览器执行。
不制作浏览器交互回放、页面回放、HAR 或 WARC 采集系统。

| 资源或动作 | 所有者 | 借用者与限制 |
|---|---|---|
| XHS 精确租约、轮次临时 session | XHS runner | worker 使用指定资源，不换号、不持久化跨轮登录态 |
| 当前 browser/context/page 实例 | 原执行位置的 runtime/worker | login/sign 借用既有对象，不私建第二个 context |
| B站行为会话及 HTTP 会话 | 执行器内原有分支 | article 模块沿原关闭与请求顺序借用 |
| 进程树监督、信号和异常收束 | 原父进程与 XHS 根监督 | 平台只报告错误，不接管其他进程树 |
| staging 产物 | 原生成进程 | 提交与清理由项目侧按 S11—S12 管理 |

正常关闭与异常清理不能只迁 `core.close()`；现有 main、执行器和 XHS 根监督均有职责。
XHS 保持单轮一个临时 profile、一次 Chrome 启动、一个 BrowserContext；失败不轮内换实例。
已有 context 上的 login 操作不得自行再开 context；页面与标签页动作保留平台特有行为。
XHS 扫码刷新锁存、二维码时限、人工等待和标签页保留规则以平台文档为准，不重新设预算。

平台有限详情/作者/图片重试、执行器策略、根运行超时分别登记拥有者及包含关系。
重试层只能消费自己的既有预算；嵌套调用不能各自重新获得全额总预算或形成次数乘法。
通用 retry wrapper 不得隐藏重启、fallback、自动换号或重新建链；错误来源必须保持可判别。
现有已授权的站内请求 fallback 可保留并显式命名，不得借同名机制添加浏览器重启。
网络等待时钟、ACK 超时、人工等待、lease 与总超时的关系需单独核验，不能统一成一个 timeout。
现有 Tenacity 在 XHS、微博、知乎 client 的次数、等待和异常传播不同，逐接缝登记，不统一默认值。
HTTP 超时/状态异常、Tenacity 最终异常、解析/模型校验异常、Pillow 解码异常分别映射到 S13，
保留原始阶段和候选级/运行级区别；不可因包装失败降为正常空结果，也不直接输出原异常载荷。
替换库时须核对库内、平台、执行器与根监督的实际尝试总数和墙钟上限，不能让嵌套预算相乘。

## S10｜纯解析、清理与证据出口

parser 先以原响应的内存对象解析，可读取识别来源所需的原字段；不直接落盘原响应。
在尚能识别头像键及其 URL 的阶段显式 sanitize，再投影为记录或可输出的观测表示。
禁止先丢弃头像键、留下重复 URL，再声称仅凭投影结果可以完成清理。
纯解析指不发请求、不写库、不取全局状态；不要求原响应本身预先改造成新 schema。
Parsel/Selector 和 Pydantic 可留在解析内部；项目契约接收的是有来源的投影，不是外部库对象。
HTTP 请求序列化输入也须固定：方法、路径、查询顺序/编码、JSON 或 form 的表示、空值省略规则、
body 字节及签名所见输入应与实际发送一致；不能改成模型 dump 后悄悄改变签名或请求含义。

第一次项目序列化前必须强制递归清除已知头像键及同记录中由这些键证明的重复 URL。
这一出口约束覆盖 JSONL、manifest、events、摘要、stdout/stderr 项目日志与离线 fixtures。
parser 清理不是唯一防线；serializer 仍须失败关闭，不能因新调用点绕过旧 writer 而泄漏。
异常消息和日志参数也可能携带响应片段，必须先转成允许输出的清理结果。
原始响应留存不在本次范围，staging 不要求也不应因本设计增加原始响应归档。

离线验证只使用经授权且清理后的样本，保留用于验证来源和缺值的必要结构。
没有适用样本就标“待补”，不以在线抓取、浏览器回放或额外脱敏采集系统填补本轮缺口。
清理不能将解析失败改成合法空数组，也不能剥掉来源字段后让后续校验误判已观察。
缺键、null、解析错误、已观察的空数组/空串/0 分别保留；不可用 truthy 默认值合并这些状态。
发布时间必须保留平台原始发帖时间与转换依据；缺时间不能落入 localtime(None) 或采集时钟补值。

## S11｜图片、文件与内容事务

图片下载鉴权留在平台和原 session transport，保留 headers、Cookie、referer、代理及请求方式。
worker 负责下载 staging 与 manifest；B站由执行器内对应平台分支承担相同逻辑职责。
“child”表示原产物生成方，不暗示为 B站增加子进程。
执行器侧 artifacts 独立检查路径、文件字节、大小、类型、哈希、权威来源与同帖 SHA 去重。
校验通过后晋升到正式媒体路径，项目 db 接口执行内容事务；URL-only 不构成正文图完成。

| 边界 | 输入与操作 | 失败后的最低要求 |
|---|---|---|
| 下载/初验 | 平台候选和原鉴权会话 → staging/manifest | 保留原错误分类，不用占位文件冒充成功 |
| 独立复验 | manifest 与实际字节、路径关系 | 拒绝缺失、越界、篡改或不完整整帖 |
| 晋升 | 已验证图片 → 正式路径及本轮新增文件清单 | 区分复用旧文件与本轮新文件 |
| ContentCommit | 记录、图片关系与正式门禁 → SQLite | 记录提交是否确定，供清理决策使用 |

文件系统与 SQLite 不是一笔全局事务，不能笼统写“失败全部回滚”。
commit 前确定失败时，只可回滚本轮新增且符合现有清理约束的文件，不删复用媒体。
commit 后或提交结果不确定时，不得盲删可能已被数据库引用的文件；先按现有实现确认结果。
`FormalImportBeforeCommitError`、`commit_formal_import` 与媒体 rollback 包装是需保留的接缝。
v0.2 审计定位了部分晋升失败和 SQL 失败路径的测试断言，当时未运行，见 V08；不能据此宣称全部掉电窗口已覆盖。
不得先持久化图片 URL 再计划补图，也不得为减少事务复杂度取消独立字节复验。

## S12｜三个提交边界与 XHS ACK

| 边界 | 内容 | 所有者 | 与正式成功的关系 |
|---|---|---|---|
| staging snapshot | JSONL/manifest 不可变快照、图片引用和摘要/pointer | XHS exporter 发布，root 验证 | 只是完整批次的恢复证据 |
| DiscoveryCommit | scope 内 checkpoint、seen、累计摘要引用及审计事件 | 通用执行器或 XHS root，各沿原路径 | 不等于内容入库，也不等于耗尽 |
| ContentCommit | 通过最终门禁的累计内容及媒体关系 | 执行器 application + 项目 db | 是正式完成必要条件之一 |

XHS 当前协议见 `src/trippostcollect/xhs/batch_checkpoint.py`，不得改写成普通日志回调。
`publish_batch(event)` 的前置条件是完整批次所有 store 已 await，且事件已持久到 execution state。
既有 hook 先写 `adaptive_batch_completed` 再调用发布；迁移后应显式调用而不改变顺序。

1. exporter 只为合格的完整非耗尽批次发布；未完整尾批和耗尽批次不能借此提前推进。
2. 固化 JSONL/manifest 快照并 fsync，保持相对 staging 图片路径；累计旧摘要的引用继续携带。
3. 写批次 summary、pointer 并按现行原子写入及目录同步协议持久化，然后等待匹配 ACK。
4. root 的 `BatchCheckpointCommitter` 校验 run/account/target/query scope、序列、哈希和安全边界。
5. root 复验产物、当前状态、完整事件及精确租约，在自己的 SQLite 事务内调用
   `commit_child_discovery(imported_completion_verified=False, commit=False)`，并记录批次提交事件。
6. 事务成功结束后才写 ACK 并同步目录；child 收到匹配 ACK 才继续后续批次。

这是最终内容入库前的发现记忆提交。不能把所有 checkpoint 一律移动到最终 ContentCommit 后。
完整 refresh 全部命中已知 ID 时，可无本轮文件而发布合格摘要；不能虚构 JSONL 或重置 deep。
声称有有效记录却缺文件则失败；已下载图片仍按现行快照校验同步其字节。
快照不复制 Cookie/profile/browser storage，也不是原始响应档案。

背压、ACK 等待上限和匹配条件保持现状；超时是运行级失败，不是网络重试或耗尽。
重复 pointer/sequence 不得重复推进，旧 ACK 不能确认新 pointer；ACK 丢失不能导致漏候选。
现有代码对同进程序列去重、提交后写 ACK 的处理需原样保留；跨崩溃幂等及重放边界仍需故障验证。
不能把“保留幂等语义”误写成已有任意崩溃点 exactly-once 证明，验证缺口列于 V07。

正式失败不得把未通过最终门禁的累计内容入库，但可保留先前完整批次的合法恢复证据。
XHS `operator_interrupt` 按当前 guard 禁止新增推进；已合法提交和 ACK 的历史恢复点不回滚。
guard 的检查与事务线性化竞态必须结合现有 terminalizer 测试核验，不虚构更强信号原子性。
未完整尾批不 seen、不越过；通用失败后的安全摘要提交与 XHS 信号处理不能合成一个布尔开关。

正式契约/架构中概括“中断不推进”的措辞，应理解为不因中断再新增推进，不能用于抹除先前 ACK。
这是基于更具体的 XHS 批次文档、实现和测试的迁移解释，不是本轮更改冻结规范。
若评审认为概括措辞仍有歧义，登记 G02 治理澄清；当前不修改冻结文件，不自行补签历史状态。

## S13｜错误和结构化控制事件

| 错误范围 | 代表来源 | 目标处理 |
|---|---|---|
| 运行级 | 登录/安全验证、频控、搜索失败、browser 关闭、ACK/本地证据异常 | 停止并保留准确原因，不降为候选失败 |
| 候选级 | 正文、作者、正文图在适用有限重试后仍失败 | 沿现行 candidate_skipped 及批次完整性规则 |
| 决定性字段无效 | 权威响应已经证明不满足字段要求 | 与解析失败区分，不伪造来源证据 |
| 非目标内容 | 视频或其他现行明确跳过对象 | 跳过而非失败，不新增视频请求 |

错误应携带 platform、阶段、候选标识（如适用）、来源类别、现行错误码和可清理的说明。
正文、作者、图片的失败不能互相代替；parse exception 不允许转成 `[]`、`0` 或 has_more=false。
平台报告错误观测，application 按正式契约和平台细则裁决，db 不根据异常名称自行推进。

影响状态、批次发布、推进或停止的结构化事件必须显式调用、同步完成且允许失败向上传播。
不能 fire-and-forget，不能只写 stdout 后期待结束扫描补回，也不能由日志 handler 获得提交权。
observability 日志只辅助诊断；其输出失败如何处理沿现有策略，不发明新的控制信号。
迁移需逐个区分控制事件与普通日志，保留控制事件顺序、持久化时点及错误传播链。

## S14｜入口兼容、命名和冻结命令

目标包入口可用 `python -m trippostcollect.platforms.entry` 替代内层 runpy，上述模块名是建议。
bootstrap 选平台并装配实现，不再执行上游 main/cmd_arg/config 初始化链。
外层正式 CLI 可保留薄包装；内部模块不得反向 import scripts，也不靠 cwd/sys.path 注入寻址。

| 名称或接口 | 目标处理 | 条件 |
|---|---|---|
| crawl_runner / xhs_runner 正式入口 | 保留语义与账号隔离 | 内部装配可迁移 |
| mediacrawler_crawl 外层执行命令 | 可保留薄包装 | 不再包含平台业务实现 |
| mediacrawler_export_entrypoint 私有桥 | 被新包入口替代后移除 | 旧未完成轮次已完成或安全终止 |
| 其他在用辅助 CLI | 先核查调用，再保留转发或另批变更 | 不能承诺“全部脚本名永远保留” |
| `job_kind=mediacrawler_search` | 保持 | 现有 schema 与存量行契约 |
| `outputs/mediacrawler_runs/` 等产物路径 | 保持 | 历史证据及读取方依赖 |
| 废弃参数/字段 | 继续拒绝 | 不新增兼容层 |

wire/env、外部字段、目录和现行命令的保留，与私有内部函数名的删除应分开审阅。
新轮次冻结新的 child command；旧已签 command 不静默改写、不重签来适配新入口。
历史完成产物继续可读，不为验证迁移而重跑；未完成旧轮的处理见 S19。
名称保留只是兼容既有契约，不能据此认为仍依赖上游运行树。

## S15｜精确源码映射与闭包边界

下表 `C` 为 `scripts/mediacrawler_crawl.py`，`E` 为 `scripts/mediacrawler_export_entrypoint.py`，
`M/` 为 `tools/MediaCrawler/`。仅“已定位活跃接缝”获静态证据，不等于整文件全部存活。
目标栏表示职责归属建议；函数拆分不得在首次机械迁入时同时更改行为。

| 编号 | 精确源位置 / 函数 | 状态与目标 |
|---|---|---|
| M01 | `C:5020 run_bilibili_article_search`；`:5601 _run_platform_without_policy` | 已定位正式 article 分支；迁入 B站包，原进程调用 |
| M02 | `C:5622` worker 命令；`E:724 main` | 已定位执行器→worker→runpy；改包入口，保持层级 |
| M03 | `C:1002 run_bilibili_behavior_session`；`:4780 fetch_bilibili_article_detail`；`:4861 hydrate_bilibili_article_record` | 已定位 article 行为/正文；runtime 与 B站能力分工 |
| M04 | `C:4630 download_bilibili_record_images`；`:4949 fetch_bilibili_article_page`；`:4979 fetch_bilibili_follower_count` | 已定位图片/搜索/作者；实际请求依赖待闭包核查 |
| M05 | `E:103 sanitize_export_item`；`:117 install_export_hook` | 已定位清理和 writer 替换；records/serializer 显式出口 |
| M06 | `E:143 install_batch_checkpoint_hook` | 已定位控制 hook；显式事件→publish，不变为日志订阅 |
| M07 | `E:298 install_xhs_repair_resilience`；`:446 install_douyin_browser_detail_fallback`；`:576 install_weibo_browser_detail_fallback` | 已定位 hook；分别归对应平台，保留修复与正式范围区别 |
| M08 | `M/media_platform/weibo/core.py:115`、`douyin/core.py:102`、`zhihu/core.py:233`、`xhs/core.py:1662` | 审计定位启动链；core/client/login 闭包逐站核验 |
| M09 | `M/main.py:122` | 已定位正常关闭分派；与父监督一起迁，不只搬 core.close |
| M10 | `M/media_platform/zhihu/core.py:54`；`M/tools/trippostcollect_behavior.py:28` | 已定位反向依赖；改注入与共享包调用 |
| M11 | `M/cmd_arg/arg.py:369`；`M/tools/trippostcollect_adaptive.py:28` | 已定位全局配置写、SQLite 读；S07/S08 隔离 |
| M12 | `M/media_platform/zhihu/client.py:202`；`help.py:254` | 已定位图片会话与 execjs；保留 transport 和 JS 桥 |
| M13 | `C:2773 validate_formal_record`；`:6289 apply_formal_completion_gates` | 已定位字段与完成门禁；application 保持唯一裁决 |
| M14 | `C:3896 materialize_formal_record_images`；`:4335 import_valid_records`；`:4413 import_valid_records_with_media_rollback` | 已定位复验/事务/回滚；分到 artifacts/db 并保留编排 |
| M15 | `C:2951 persist_discovery_checkpoint`；`:7153` 调用位置 | 已定位通用发现提交；不能和 XHS signal guard 硬合并 |
| M16 | `src/trippostcollect/xhs/batch_checkpoint.py:86 publish_batch`、`:198 BatchCheckpointCommitter`；`scripts/xhs_runner.py:1121` | 已定位 XHS 发布/提交/安装；最后专项迁移 |
| M17 | `scripts/mediacrawler_login_warmup.py:517 main_async` | 显式检查 `MEDIACRAWLER_DIR.exists()`；删除运行树前须替换该存活辅助入口的依赖检测，不能仅检查 import 就宣布独立 |

尚待闭包核查：各站 model/constant/helper、browser launcher/CDP、sign JS、异步 writer、store、
proxy/cache、下载重试、资源装载、动态 import、诊断/详情修复/登录入口和子模块测试依赖。
不能按旧目录名断言整个 store 可删：其中下载、manifest 和写出能力可能仍属必需角色。
同理不预判所有 proxy、cache、aiofiles、requests 均可删；只删除已证明无存活引用的实现。
上游 B站视频/动态代码不是正式 article 替代物；其能否删除仍须辅助入口闭包结论支持。

## S16｜依赖、安装与第三方来源

复用优先级为：已验证的现有库/本地 fork 实现 → 接口窄且维护可靠的包 → 许可明确的小块收编
→ 确属项目语义才自研。现有实现已有同等能力时，不因候选项目写法新颖再做一次。
解除上游运行树依赖不等于零第三方依赖；研究新 HEAD 不构成升级当前锁定版本的理由。
包依赖、代码切片收编和方法借鉴分别评审，不能把“参考过”写成“可直接复制或整包引入”。
已有构件与精确调用见 S26；30 项外部候选的证据、限制和采用级别见 S23—S25。

建立从正式入口及获准保留辅助入口到模块、Python 包、JS、其他资源的引用闭包。
静态 import 只是起点，还需动态导入、运行期资源路径、选项分支和平台延迟加载验证。
迁入的功能角色决定依赖保留，不能照抄上游 requirements 或按包名白/黑名单裁剪。
不把开区间升级依赖作为搬迁方案；以当前可用版本和锁文件关系评估根 requirements/lock 的合并。
Python 3.11 兼容性、原 driver 包及 Node/execjs 运行要求按实际闭包和安装测试确认。
requests 虽有现有声明，存活调用闭包未核验；lxml 的传递关系与独立调用也待核，均不能先判可删。
不为洁净架构重写校验器、HTTP 栈、哈希、SQLite 或原子文件 API；边界适配只处理项目需要的语义。

JS、stealth 等实际存活资源需成为可安装 package_data，装载不依赖仓库相对目录。
必须在仓库外干净目录安装构建产物，验证选定平台导入、资源读取和根依赖完整性。
验证环境不得借当前 checkout、子模块 cwd 或隐式 PYTHONPATH 补齐缺失文件。
逐平台检查未选平台不被初始化；“按需加载”不等于发布包可以漏掉正式支持平台所需依赖。
保留第三方许可证、版权头和必要来源记录；收编不消除保留义务，本文不展开法律结论。
上游未来可作为人工参考，不作为运行时下载、定期强制合并或构建所需目录。

## S17｜分阶段迁移与退出门禁

以下是内部工程拆分，尚未批准实施；每阶段只在入口可切回、状态兼容清楚后进入下一阶段。
助手可按闭包与样本条件安排微博、知乎、抖音等内部先后，不再把先行站点作为用户待选事项。
首期验收范围始终是五站完整交付，P01 的单路径只用于工程验证，不能作为首期交付或停用其余平台的理由。
B站 article 的迁移成功不能证明已经脱离 fork，因为它原本就绕过上游 B站 worker。

| 阶段 | 工作范围 | 进入下一阶段的证据 | 代码回退单位 |
|---|---|---|---|
| P00 | 保留 B01/B02 历史基线，当前修复验收用 B05/B06；列在用 CLI/env/wire/path/指纹、读取时机、资源与测试清单 | 两 issue 已解决；五站闭包、精确接口及读取时点仍待齐，不自动 ready | 本轮未改运行代码 |
| P01 | 机械迁入一条真实通用 worker 路径与必要共享闭包 | 稳定字段/事件/决策差分；原命令可用；无签名/驱动/网络重写 | 单路径入口与迁入实现 |
| P02 | 逐项显式注入；清理、fallback、控制事件 hook 各自归位 | 每项读取时点、失败传播和门禁保持；反向 scripts 导入清除 | 每个独立接缝 |
| P03 | 其他通用站点及根 B站 article 分别迁移 | 逐站离线与事务验证；article 仍原进程、完整正文来源正确 | 各站独立切换 |
| P04 | XHS 最后迁入，专项验证登录生命周期/ACK/信号/租约 | S12 全链及故障窗口验证；无第二 browser/context | 整条 XHS 链协同回退 |
| P05 | 完整闭包、仓库外安装、根依赖与旧轮次清点 | 无上游运行引用；旧轮安全处置；治理 diff 获授权并同步 | 删除前版本与安装产物 |
| P06 | 删除上游运行树并完成同批文档切换 | 目标态验证及证据齐备，才可报告对应范围完成 | 保留可恢复旧代码资产 |

阶段编号不等于提交次数；每阶段可拆审阅单元，不承诺一个大提交解决整站。
P01—P05 未获得该阶段正式切换及治理授权前，只在评审分支/隔离验收环境切换，不更改正式默认路径。
未来若获准分步切换，每步必须独立满足 P06 式切换门禁，并同步受影响权威文档；
任何时点均保持五站现有正式功能，未迁移站继续使用现有链路。分步安排不缩减首期五站验收范围，
也不允许先改生产后补文档。
过渡 monkeypatch、全局 config/env 读取须列明调用点、行为理由、目标端口及退出门禁。
P01 可短暂保留既有机制以降低变量；P02 处理首站，P03/P04 处理后续站点。
P05 前所有存活路径必须消除过渡 hook、内部任意 env/config 读取和 sys.path/runpy 桥。
兼容旧外部 env 的入口解析仍可保留；这与内部全局读取清除不矛盾。
任何门禁失败停在当前阶段，保留旧代码可执行，不以删测试、放宽来源判据或缩减图片门禁过关。
P00 须对 S27 的每个实际拟采用构件记录真实缺口、许可链、切片闭包、Python 3.11 及副作用；
P01—P04 逐接缝比较第三方错误映射、缺值/0/空、真实发帖时间和 HTTP 序列化输入。
离线差分必须禁意外网络；P02 的显式注入不能新增默认重试或使原预算相乘。
这些均为未来门禁；v0.4 的文档、保护性测试及现有代码隔离验证见 P00，不安装新候选、不运行新候选的测试或初始化代码。

## S18｜验收矩阵与证据强度

以下现有函数和行号来自 v0.2 的 B03 审计；当时只确认定位并阅读部分断言，未执行测试。
v0.4 的新增测试与 v0.5 同步的实际运行结果见 P00 工作单，不在本表补签历史结果。
当前唯一可复用测试方式为 [可复用测试运行](testing.md)；P00 的旧实验命令仅用于追溯。
main `b7e52db` 的 [36333548215 验收](https://github.com/ottercoconut/TripPostCollect/actions/runs/36333548215)
在托管 macOS 26.6.2、Python 3.11/3.12 各自通过 component 878、socket 1、installation 1、真实 OS 51，
根合计 931；fork 另 417（32 文件），所有失败/errors/skipped/xfail/xpass 为 0，隔离与清理产物已核对。
首次 main `b2db8b7` 的 36330351689 watchdog 时序失败仍保留为历史；PR #4 `209f671` 的有界 ready/release
与纯时钟 deadline 修复经独立 Codex 审阅无阻塞，随后才获得上述全新主线通过结果。
本机 26.5.2 / Python 3.12.13 的主线新副本只验过 component/socket/installation；误用 Documents 原 venv
导致 exec 被拒、pytest 未启动的准备错误已通过独立临时锁定环境纠正，不代表本机 OS 通过。
准备分支保留的额外 56 例与 main 组成隔离副本后，本机 component 934/934，收集 987、deselected 53；
其余 lane 此轮未重跑，不能写成 main 或双版本 934。具体摘要路径与证据边界见 P00。
目标验收要逐项核对实际断言；单个正例不代表五平台完整负例矩阵。
`M/` 缩写沿用 S15。缺口必须明确补证，不将计划写成已通过。

| 编号 | 现有测试路径:行与实际函数名 | 目标正反例及覆盖边界 |
|---|---|---|
| V01 | `tests/test_mediacrawler_pagination.py:223` `test_unfinished_pagination_is_not_reported_as_source_exhausted` | 有停止证据 vs 分页未结束/阻断；五站阻断误耗尽组合待验证 |
| V02 | `tests/test_discovery_checkpoints.py:49` `test_query_fingerprint_ignores_runtime_budget_but_tracks_source_options` | 来源变化换指纹、预算变化不换；旧序列化兼容与并发读取可见性待补 |
| V03 | `tests/test_discovery_checkpoints.py:651` `test_bilibili_frontier_starts_at_saved_page_and_skips_known_author_lookup` | 已知候选提前跳过；中途失败安全 frontier、refresh/deep 全矩阵待验证 |
| V04 | `tests/test_author_avatar_sanitization.py:184` `test_mediacrawler_exporter_sanitizes_before_jsonl_serialization` | 首次 JSONL 清理；manifest/event/log/fixture 全出口及重复 URL 负例待验证 |
| V05 | `tests/test_post_detail_repair.py:327` `test_repair_fallback_makes_sparse_zhihu_detail_formally_valid` | 仅有关修复正例；五站 fields/source、followers=0、缺 count/source/observed 均需独立核验 |
| V06 | `M/tests/test_cdp_browser_lifecycle.py:754` `test_manager_rejects_second_launch_attempt` | 第二次启动拒绝；单 Context、login 借用、page 关闭不 fallback 待逐断言核验 |
| V07 | `tests/test_xhs_batch_checkpoint.py:143` `test_exporter_stops_without_ack_and_does_not_write_sqlite` | 无 ACK 停止、child 不写库；重复/旧/丢失 ACK、提交后崩溃、空 refresh、哈希/租约拒绝待验证 |
| V08 | `tests/test_image_materialization.py:262` `test_promotion_failure_after_replace_removes_new_target`；`tests/test_image_persistence.py:350` `test_one_missing_materialized_file_prevents_new_post_and_sql_failure_rolls_back` | 缺文件阻入库、晋升/SQL 失败；beforecommit、committed、提交不确定及复用文件保护待验证 |
| V09 | `tests/test_bilibili_article_detail.py:66` `test_search_excerpt_alone_is_not_a_formal_bilibili_record` | 权威正文与搜索摘要区分；迁入 article 路由且不走上游视频/动态待验证 |
| V10 | `M/tests/test_zhihu_detail_images.py:129` `test_failed_or_unparsed_detail_cannot_enter_image_success` | 失败/未解析不能伪装图片成功；各站正文/作者/图片错误来源矩阵待补 |
| V11 | `tests/test_xhs_leases.py:1982` `test_normal_end_releases_exact_lease`；`tests/test_xhs_terminalizer.py:93` `test_interrupt_before_linearization_rolls_back_event_and_checkpoint` | 精确租约与线性化前中断；signal 后禁新增推进且保留先前 ACK 的联动待验证 |
| V12 | 未由 B03 定位具体函数 | 抖音空首屏：健康空 vs 页面仍有内容/受限/含糊响应；page/offset/search ID 不误推进，待补覆盖清单 |
| V13 | 未由 B03 定位具体函数 | 仓库外安装、package_data、Python 3.11、selected 懒加载与 root dependency；待建验证证据 |
| V14 | 未由 B03 定位具体函数 | 控制事件写失败必须传播；运行级与候选级不混淆；新旧 CLI/env 默认和旧 command 处置，待补 |
| V15 | v0.3 研究提出，当时未执行 | HTTP/重试/解析/校验/图片异常映射；缺键/null/0/空分开；平台时间不补当前时间；签名前后请求序列化输入一致 |
| V16 | v0.3 研究提出，当时未执行 | 库内与外层重试总预算不相乘；未匹配 HTTP 请求失败关闭；无 pass-through、空 200 兜底或意外联网；静态依赖约束见 S25 |

离线 old/new 比较使用相同清理后 fixture，比较稳定字段、来源、事件顺序、候选决策和前沿决策。
归一白名单仅包含采集时钟元数据、run_id、临时绝对路径，且只可在比较副本中按显式规则处理；
不改真实证据、摘要链或哈希。`published_at` 及其原始平台时间、来源字段、查询指纹、cursor/search ID、
正文、图片候选顺序、哈希、错误码、事件顺序均不能被忽略或重写。
涉及计时/重试/超时的测试必须注入相同可控时钟/随机源，验证预算与次序，不能抹去时间差掩盖行为变化；
这一控制仅用于测试，不改变正式随机/节奏。
同一确定输入可比较序列化稳定部分，不承诺跨在线轮次逐字节相等。
先建立“应相同字段/允许变化字段/不能忽略字段”清单；未知差异必须解释，不能一律去掉再比较。
HTTPX 内建 MockTransport 能满足时无需新增库；复杂匹配才评估 RESPX，静态 import 图评估 Import Linter。
离线测试的 HTTP、urllib、其他客户端及浏览器入口分别隔离，MockTransport/RESPX 不覆盖全部网络栈。
测试环境另设网络失败关闭约束；缺匹配即失败，不回退真实端点、不自动录制，也不启动浏览器。
VCR.py 仅考虑已净化/合成 cassette 的 record_mode=none；工具范围与风险见 S25，不形成回放系统。

未来验证应包括受影响测试、必要根回归、临时 SQLite/文件故障注入、安装资源及冻结校验。
测试路径迁移按存活角色决定，不能用旧测试文件总数证明覆盖，也不照搬无关依赖测试。
测试必须随模块重构：冻结业务断言，不冻结测试文件、私有函数、旧脚本名或 monkeypatch 架构。
分为 contract/纯解析、各站 adapter（仅请求/解析/序列化/平台错误的 fixture 差分）、运行控制组件、OS integration、packaging/CLI smoke；run-ID 测稳定纯接口，完整入口导入另测。
业务测试经已有 inspector 或未来窄端口注入无残留、存活、未知、扫描报错等具体观测，禁止全局放行安全门禁；
真实 gate、信号和 socket 独立验证。删除旧 hook 结构断言前，须有新显式出口的脱敏及失败传播测试替代。
原 29/30 红项的精确节点、置信度、正常使用影响与分阶段门禁见 [P00 失败复评](platform-adapter-preflight.md#失败复评与测试迁移)，按原复评时点理解。
它们不是当前 30 项未决 bug；#1/#2 已解决，不再阻塞 P00。OS/安全清理在未来对应边界改动或正式切换前仍须适用验证，不补签历史结果。
dry-run 仅验证部分计划构造，且通用模式可能写调度表/摘要/state；本轮不运行。
`--no-import` 不证明正式内容事务或发现提交成立；XHS 批次握手也不由它覆盖。
正式试跑需另行授权具体平台、任务范围、账号方式与运行窗口，不在本稿授权内。
不得为“低频小范围验收”引入数量停止完成语义；人工中途停止只能按真实失败/中断状态验收。
只有符合正式契约的 `source_exhausted_met=true` 及对应停止事件和全部门禁才可报告正式完成。

## S19｜回退与旧轮次处置

代码回退与生产状态恢复是两件事；可回退代码不意味着可 git 回滚数据库或覆盖媒体目录。
不删除已 ACK 快照、已提交媒体和累计摘要引用，不手改 checkpoint、不解冻或补签 execution state。
回退前核对旧代码能否读取新轮合法产物；若不能，停止切换并保留当前证据，按既有恢复流程处理。

| 对象状态 | 处置 | 禁止 |
|---|---|---|
| 历史已完成轮次 | 保持原冻结命令与产物可读 | 改 command、重跑以伪造兼容证明 |
| 真实未完成旧轮次 | 用旧入口完成，或安全终止后按既有 checkpoint 新轮恢复 | 在原轮中途换运行树或私改状态 |
| 新入口新轮次 | 冻结新 command，沿既有恢复规则运行 | 把旧私有桥永久保留成兼容负担 |
| 已 ACK 但未最终入库 | 保留快照、哈希、累计引用与发现记忆 | 因 ContentCommit 尚未发生而清空进度 |
| commit 结果不确定 | 依 S11 核验引用和事务结果 | 直接清理所有本轮晋升文件 |

旧运行完成或停止前不得删除它依赖的运行树、环境和资源文件。
删除树前的盘点不访问无关凭证；具体生产状态核对属于未来获准实施阶段，不在本轮执行。
回退恢复的旧入口只服务其明确范围；新目标生效后不维持两套相反的常规操作流程。

## S20｜治理窗口与资料路径计划

v0.4 前期修改权限限于设计、版本管理、P00 工作单、指定保护性测试与必要讨论导航；其后 #1/#2 的实现及合并单独获准并完成，不扩展为平台重构授权。
v0.5 本轮仅两份文档的状态与证据同步；文档编辑子任务不执行 Git 写操作，主助手按用户授权审阅并提交；中文 commit/issue/PR 与标准主题分支约束不变，详见 P00 范围。
不包含 AGENTS 和以下四个登记资产：
`docs/admin-client-record-workbench-template.html`、`docs/formal-crawl-contract.md`、
`docs/crawl-architecture.md`、`docs/data-persistence.md`。
`config/frozen_files.json` 不更新；治理授权和冻结解除必须明确对应文件及可审阅 diff。

| 编号 | 资料路径 | 未来处理时点与范围 |
|---|---|---|
| G01 | `docs/crawl-architecture.md` | 授权后同步实际新拓扑与包职责；不是预先宣布迁移完成 |
| G02 | `docs/formal-crawl-contract.md`、`docs/data-persistence.md` | 若需澄清批次提交与中断概括措辞，依据 S12 具体协议和测试审阅 |
| G03 | `docs/operations-runbook.md`、`docs/platforms/*.md` | 同步真实入口、恢复步骤和平台接缝；不重定义共享规则 |
| G04 | `docs/mediacrawler-fork-maintenance.md` | 切换前仍是现状维护流程；目标生效同批标为历史/只读或移出当前流程 |
| G05 | `docs/README.md`、字段覆盖/数据字典 | 按实际受影响链接和职责更新；无字段语义变化不扩改 |
| G06 | `AGENTS.md`、`config/frozen_files.json` | 仅明确治理授权后按对应变更同步核验；本轮不触碰 |
| G07 | `docs/platform-adapters.md` | 保留设计/批准/实现状态区分；不以静态审阅冒充验收报告 |

不允许先删除运行树、再留下与现行架构矛盾的“已完成”状态等待以后补文档。
切换的代码、依赖、测试和受影响权威文档需在获准窗口协调生效，未批准时停在切换前。
此要求适用于 S17 的每次正式切换，并非全部等到 P06 才更新文档；逐站正式切换须逐站满足同等门禁
及治理授权，未获授权的阶段切换仅限评审分支/隔离验收环境，不改变正式默认路径。
未来治理变更按授权执行不可变标志与登记哈希流程；当前真实冻结资产不解冻。
隔离副本恢复 uchg 元数据与最终测试证据见 P00，不等于修改原件或登记哈希。
上游合并流程与本地自主维护不能同时作为当前必走流程；G04 是目标切换门禁之一。

## S21｜决策记录

| 编号 | 状态 | 结论 |
|---|---|---|
| D01 | 用户已确认 | 五站可用性、行为、证据保持；解除上游运行/配置/目录/持续合并依赖；用户全责维护 |
| D02 | 用户已确认 | Chrome for Testing、原 Playwright/Patchright 路径与原请求方式保留 |
| D03 | 用户已确认 | 知乎 execjs+JS/桥接/运行时保留；无浏览器交互或页面回放 |
| D04 | 用途已确认；工程约束 | 用户已确认科研无商用；第三方许可证保留属于工程约束，不代表用户另行授予来源权利，不展开法律判断 |
| D05 | 设计约束 | 原进程形态和 S12 批次协议不变；逻辑分层不等于新进程或新 schema |
| D06 | 用户已确认 | 统一规则与结果，保留平台特有执行流程，不强迫五站统一完整抓取循环；强调解耦模块化 |
| D07 | 未来权限门禁；尚未授权 | 先完成适用离线验收；具体正式试跑范围及窗口须另获授权，不是当前待用户解决的技术问题 |
| D08 | 未来权限门禁；尚未授权 | 治理变更须在迁移 diff 可审阅时获得明确解冻授权，并与目标切换同步；当前不解冻 |
| D09 | 工程待核验 | 引用闭包、SQLite 读取时点、版本锁/安装资源、ACK 崩溃窗口和各站覆盖缺口 |
| D10 | 用户已确认 | 广泛拆解五站及类似科研采集项目，优先复用成熟构件，不自行重写已有可靠基础能力 |
| D11 | 助手技术评审 | 模块拆分粒度、窄端口与必要 Facade 的位置由助手依据实际 diff 与证据判断；只补缺口，不建平行框架；测试随模块迁移，保留保护语义而非旧文件/hook，分层及失败复评见 S18/P00 |
| D12 | 用户已确认 | 首期五平台完整交付并保持正常抓取能力；内部可分步，不交付单站试点，不逐站减损正式功能 |
| D13 | 助手技术评审；本轮不重开选型 | 保留 S26 研究与采用边界，S27 工程事实仍须补证；新候选不安装，不自动新增依赖 |
| D14 | 两 issue 已获准、验证并合主线 | #2 随 PR #3、#1 经 PR #4 修复后均关闭；不再是 P00 阻塞。仅这些修复实现获准，平台 adapter 重构仍未授权，P00 完整闭包和精确接口、读取时点仍待核验 |

D07/D08 的真实采集与治理授权尚无；D09/D11/D13 由助手承担工程核验和技术评审，不要求用户技术评审。
D12 已确定五站首期范围；本稿及保护性测试完成均不表示实施、上线或治理解冻获准。

## S22｜一致性检查与本稿验收边界

| 约束 | 唯一责任所有者 | 相关段 | 验证入口 |
|---|---|---|---|
| 正式完成语义 | 正式契约；application 执行裁决 | S02/S04/S13 | V01/V05、正式门禁 |
| B站真实 article 路径 | 执行器内 B站能力 | S03/S15/S17 | M01—M04、V09 |
| 游标解释与推进批准分开 | 平台解释；application 批准 | S06—S08 | V02/V03/V12 |
| 首次序列化清理 | records/serializer 出口 | S06/S10 | V04、多出口负例 |
| 浏览器资源和预算 | 原 runtime 所有者与根监督 | S09 | V06/V11 |
| 内容事务与媒体清理 | 执行器 application + artifacts/db | S11 | V08、提交不确定故障 |
| XHS 批次发现事务 | XHS root committer | S12 | V07/V11 |
| 通用失败安全前沿 | 通用执行器发现提交 | S07/S12 | V03、完整/尾批对照 |
| wire/path 与新入口 | bootstrap/serializer | S06/S14/S19 | V14、旧新差分 |
| 自包含安装与依赖删除 | bootstrap 装配及构建闭包 | S15—S17 | V13、仓库外安装 |
| 生产状态不能随代码回退 | 原提交所有者及既有恢复流程 | S19 | 快照/引用/冻结命令核验 |
| 唯一真源与冻结治理 | 对应文档责任域及用户授权 | S02/S20/S21 | 冻结验证、同批 diff 审阅 |
| 规则/结果统一，平台流程独立 | 各原 application/platform 所有者 | S04/S21/S25 | D06 与接口落点逐项对照 |
| 成熟构件复用与类型边界 | 项目 schema 与各能力边界所有者 | S05/S06/S16/S26 | 不重写基础库，不泄漏外部模型/异常 |
| 研究不等于采用或实测 | 选型记录与后续实施评审 | S23—S27 | 固定 SHA、证据等级、许可/Python/副作用记录 |

文字自检应同时检查以下反例，而非只搜索某个词是否出现：
不把全部 checkpoint 放到内容入库后；不让 B站借上游视频路径；不声称所有请求经浏览器。
不把运行失败解释为一律不能保留进度；不靠 dry-run 宣称字节等价；不要求 staging 含原响应。
不把讨论授权当治理解冻；不对同一私有入口无条件同时承诺保留和删除；不把新抽象当新进程。
交叉核查 D06/D12 已确认范围与 D11 技术评审、实施授权是否分开；不把 Python 3.12 候选写成目标 3.11 可直接依赖。
不把 B站 urllib 改为 HTTPX，不推广辅助 Patchright/Scrapling，不禁止项目自有 Pydantic 契约。
S23—S27 的外部方案不能覆盖 S02 唯一真源、S12 ACK 先于最终内容提交或 S20 治理窗口。
外部 archive/storage/session 设计不授予提交权，不增加 raw/HAR/WARC 归档，不改变 XHS 单 Context。

v0.2/v0.3 审阅范围仅为文档与静态证据，历史“未运行”声明只针对当时；
v0.4 的授权、测试准备和原验证状态集中见 P00 工作单。2026-09-27 已执行隔离测试；
原独立复评只读取并复算证据，未重跑测试、dry-run、安装、数据库故障注入、浏览器或正式平台运行。
v0.5 保留上述历史边界并另列主线修复后新验收；本轮仅两文档同步与文档 diff/链接/冻结验证，不执行测试或抓取。
静态审阅只能减少显式矛盾，不是零缺陷证明；D09、V01—V16 及 S27 缺口须按 P00—P06 相关门禁逐项关闭。
开工前需有基线、现有关键契约验证与明确迁移边界；迁移后测试、安装及正式试跑在相应阶段完成，
不要求先验证尚未实现的代码。全部适用验收证据完备且获得授权后，才可宣布迁移完成；本草案仍未授权实施。

## S23｜复用研究范围与证据等级

以下为 **2026-09-27 v0.3 研究时点**的记录，v0.4 保留其内容、不重开外部选型。范围是五站相关实现及相似科研采集、提取、归档与测试工程的
30 个候选仓库元数据和选择性源码审阅，不是穷尽全部开源项目，也不是全依赖或安全审计。
输入索引登记 171 条固定 SHA 材料；登记/下载数量不等于逐份全文审阅数量，更不等于端点成功数。
早期审计报告的 149 份计数及部分“仅元数据/尚未下载”描述已过时，以下采用补充审阅后的范围。
没有真实端点测试，没有执行外部项目的安装、初始化、测试或源码内指令；外部源码视为不可信资料。
本文在正文保留具体文件的永久链接、接口概括与采用限制，未来阅读不依赖临时审计目录存在。

| 等级 | 已检查内容 | 不能据此声称 |
|---|---|---|
| E1 实现+测试文本 | 选定方法、声明、许可及部分测试代码；测试没有执行 | 测试通过、端点有效、全部异常已覆盖 |
| E2 实现 | 选定实现方法及可得依赖/许可证据；可能仍缺 helper 闭包 | 整包可安装、无隐式副作用、Python 3.11 已兼容 |
| E3 接口 | 关键接口/职责或局部实现；未深入整个调用链 | 可直接收编或替换现有流程 |
| E4 说明 | README、许可/树元数据；没有能力实现证据 | 项目宣称能力已验证或可作新 SDK |

S24 的等级针对明确读到的范围，不对仓库整体评级；“成熟、维护可靠、优先”均是选型推断。
每行的 push 日期来自该日采集的公开 `pushed_at`（UTC 日期），只表示仓库推送时间。
机器人、依赖或文档提交活跃不能证明签名修复、关键模块有人维护或目标能力仍有效。
HEAD 固定链接证明审阅对象，不证明发行包与该提交相同；正式采用须另核版本、维护响应与实际闭包。

采用级别分为：**保留**现有锁定构件；**包候选**另评开发/运行依赖；**切片候选**只评小段代码；
**方法借鉴**只取职责和反例；**不采用**表示当前范围不接入。候选不是实施授权或必装清单。
公开可读源码不一律称为可复制开源；元数据 `license=null` 也不是全仓无许可的证明。
许可结论优先引用该 SHA 文件、包声明与代码头；未见许可文件只记证据缺口，不自行裁决权利。
代码收编仍须核版权、许可链、修改标记与适用 NOTICE；科研/私用不豁免这些核验。
GPL 候选按原声明记录；未核细则不推导 only/or-later，也不把不同仓库或版本的许可互换。

## S24｜30 个候选及采用边界

跨站候选只登记一次，其他平台通过编号引用；每行至少一个固定 SHA 文件链接。
语言限制来自可得声明或 README，未知即保留未知；声明支持不等于本项目已验证兼容。

### 小红书

| 编号/候选与审阅入口 | push 日期 | 许可证据及疑点 | 语言/检查程度 | 能力与采用级别 |
|---|---|---|---|---|
| R01 [Cloxl/xhshow · client.py](https://github.com/Cloxl/xhshow/blob/b242a84f9c5a6a971bc4c90c63ad43345a4bf8a1/src/xhshow/client.py) | 2026-06-11 | [MIT LICENSE](https://github.com/Cloxl/xhshow/blob/b242a84f9c5a6a971bc4c90c63ad43345a4bf8a1/LICENSE) | [Python ≥3.10](https://github.com/Cloxl/xhshow/blob/b242a84f9c5a6a971bc4c90c63ad43345a4bf8a1/pyproject.toml)；E1，公共 API/会话及格式测试 | 保留现有签名包与调用版本；不启用实验性 SessionManager |
| R02 [ReaJason/xhs · core.py](https://github.com/ReaJason/xhs/blob/f4b62d9f8e4078e631fc6e4ec8e430bc711ee9f0/xhs/core.py) | 2025-07-01 | [MIT LICENSE](https://github.com/ReaJason/xhs/blob/f4b62d9f8e4078e631fc6e4ec8e430bc711ee9f0/LICENSE) | [Python ≥3.7](https://github.com/ReaJason/xhs/blob/f4b62d9f8e4078e631fc6e4ec8e430bc711ee9f0/setup.py)；兼容闭包待核；E1，client/测试文本 | sign 注入与详情提取方法借鉴，局部解析为切片候选；不接自有 Session |
| R03 [XHS-Downloader · image.py](https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/source/application/image.py) | 2026-09-20 | [GPLv3 原声明](https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/LICENSE)，未推定细分许可 | [Python ≥3.12](https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/pyproject.toml)；E2，explore/image/request | 图片候选拆分方法借鉴；整包不采用，CDN 重建/实况视频/TLS 默认不继承 |
| R04 [jackwener/xiaohongshu-cli · client.py](https://github.com/jackwener/xiaohongshu-cli/blob/4d63f3c0c85ccd9054fa8e96d7f761aaf2507449/xhs_cli/client.py) | 2026-03-21 | [pyproject](https://github.com/jackwener/xiaohongshu-cli/blob/4d63f3c0c85ccd9054fa8e96d7f761aaf2507449/pyproject.toml) 自称 Apache-2.0；所列树无独立 LICENSE，复制授权待核 | [Alpha](https://github.com/jackwener/xiaohongshu-cli/blob/4d63f3c0c85ccd9054fa8e96d7f761aaf2507449/pyproject.toml)；Python ≥3.10；E1，client/Cookie/sign 测试文本 | 仅方法借鉴；跨轮 Cookie、browser-cookie3/camoufox 不适配，不依赖或收编 |
| R05 [xpzouying/xiaohongshu-mcp · feed_detail.go](https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/xiaohongshu/feed_detail.go) | 2026-09-22 | [Apache-2.0 LICENSE](https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/LICENSE) | [Go 1.24.0](https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/go.mod)；E1，详情/就绪/search 与局部 helper 测试 | 页面状态提取和目标身份校验方法借鉴；不接 Go/MCP/publish 或浏览器生命周期 |

### 抖音

| 编号/候选与审阅入口 | push 日期 | 许可证据及疑点 | 语言/检查程度 | 能力与采用级别 |
|---|---|---|---|---|
| R06 [Johnserf-Seed/f2 · douyin/filter.py](https://github.com/Johnserf-Seed/f2/blob/c2c52a4da0cfe0ce646cc836738d7f1aca1308f8/f2/apps/douyin/filter.py) | 2026-09-26 | [Apache-2.0 LICENSE](https://github.com/Johnserf-Seed/f2/blob/c2c52a4da0cfe0ce646cc836738d7f1aca1308f8/LICENSE) | [Python ≥3.10](https://github.com/Johnserf-Seed/f2/blob/c2c52a4da0cfe0ce646cc836738d7f1aca1308f8/pyproject.toml)；E2，抖音 filter/model/crawler、微博 client/filter | 字段投影切片候选；开发分支不等于发布版，微博所读接口不含关键词搜索 |
| R07 [TikTokDownloader · extractor.py](https://github.com/JoeanAmier/TikTokDownloader/blob/473c90ff70c663cfb69310fff2b8d5192f200661/src/extract/extractor.py) | 2026-09-22 | [GPLv3 原声明](https://github.com/JoeanAmier/TikTokDownloader/blob/473c90ff70c663cfb69310fff2b8d5192f200661/license)，未推定细分许可 | [Python ≥3.12](https://github.com/JoeanAmier/TikTokDownloader/blob/473c90ff70c663cfb69310fff2b8d5192f200661/pyproject.toml)；E2，提取器/参数 | 缺值和时间反例、方法借鉴；整包不采用 |
| R08 [Evil0ctal/Douyin_TikTok_Download_API · dtk adapter](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/src/dtk/platforms/douyin/adapter.py) | 2026-09-23 | [Apache-2.0 LICENSE](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/LICENSE) | [Python ≥3.12、<3.14](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/pyproject.toml)；E2，当前 src/dtk adapter/base/endpoints/parser | 请求描述/解析/风险分类方法借鉴；不采用新版 runtime、服务或 identitypool |

R14 也覆盖抖音；其跨站 client 不作为替代五站执行循环的理由。

### 微博

| 编号/候选与审阅入口 | push 日期 | 许可证据及疑点 | 语言/检查程度 | 能力与采用级别 |
|---|---|---|---|---|
| R09 [dataabc/weibo-crawler · weibo.py](https://github.com/dataabc/weibo-crawler/blob/a3bfe515e9886b84151c609debdc636cbb0e9730/weibo.py) | 2026-07-22 | 当前树未见许可文件，无法确认复制授权 | [README 称 Python2/Python3，精确下限未声明](https://github.com/dataabc/weibo-crawler/blob/a3bfe515e9886b84151c609debdc636cbb0e9730/README.md)；E2，长文方法 | 长文/字段方法对照；不复制，TLS/缺值/停止条件不继承 |
| R10 [dataabc/weiboSpider · page_parser.py](https://github.com/dataabc/weiboSpider/blob/720d52a58aeff3bdafdc552b90443842ebb94ba7/weibo_spider/parser/page_parser.py) | 2026-02-04 | 当前树未见许可文件，无法确认复制授权 | [Python ≥3.6，兼容闭包待核](https://github.com/dataabc/weiboSpider/blob/720d52a58aeff3bdafdc552b90443842ebb94ba7/setup.py)；E2，PageParser | 页面职责方法借鉴；构造时请求等副作用不适配，不复制 |
| R11 [dataabc/weibo-search · search.py](https://github.com/dataabc/weibo-search/blob/b4535b71d36ae61d13ab0083a18a152914f14ba7/weibo/spiders/search.py) | 2026-06-05 | 当前树未见许可文件，无法确认复制授权 | [Python/Scrapy，精确约束待核](https://github.com/dataabc/weibo-search/blob/b4535b71d36ae61d13ab0083a18a152914f14ba7/README.md)；E2，SearchSpider.parse_weibo | 搜索页面/长文反例对照；不引其调度和停止条件，不复制 |
| R12 [mikf/gallery-dl · weibo.py](https://github.com/mikf/gallery-dl/blob/8b9a56d6b1a53bc13a80894625fd58b24de3063a/gallery_dl/extractor/weibo.py) | 2026-09-27 | [setup.py](https://github.com/mikf/gallery-dl/blob/8b9a56d6b1a53bc13a80894625fd58b24de3063a/setup.py) 明确 GPL-2.0-only，源码头 version 2；不由 LICENSE 模板例子推为 or-later | [Python ≥3.8](https://github.com/mikf/gallery-dl/blob/8b9a56d6b1a53bc13a80894625fd58b24de3063a/setup.py)；E2，微博/common/job | 图片列表与 Directory/Url/Queue 方法借鉴；不接整包及 video/livephoto/text 默认行为 |

跨站参考 R06（微博单帖/用户）、R14（身份校验及缺值反例）、R17（关键词 route），不重复计数。

### 知乎

| 编号/候选与审阅入口 | push 日期 | 许可证据及疑点 | 语言/检查程度 | 能力与采用级别 |
|---|---|---|---|---|
| R13 [KrisTHL181/zhihu-cli · article.py](https://github.com/KrisTHL181/zhihu-cli/blob/e8f14415519ef766e520f1e04a7ef3c7c0455ee3/src/zhihu_cli/content/handlers/article.py) | 2026-09-26 | [MIT LICENSE](https://github.com/KrisTHL181/zhihu-cli/blob/e8f14415519ef766e520f1e04a7ef3c7c0455ee3/LICENSE) | [Python ≥3.12](https://github.com/KrisTHL181/zhihu-cli/blob/e8f14415519ef766e520f1e04a7ef3c7c0455ee3/pyproject.toml)；E2，URL/载荷/parse/request/search | 小段 URL/载荷检查为切片候选；整包不采用，不接 daemon、会话重建或新签名 |
| R14 [ifccod/social-media-research-cli · zhihu client](https://github.com/ifccod/social-media-research-cli/blob/a1b13c9a86b1a0ea4d9d2fd79949cfc8a05b4c49/reverse/zhihu_reverse/client.py) | 2026-09-18 | [MIT LICENSE](https://github.com/ifccod/social-media-research-cli/blob/a1b13c9a86b1a0ea4d9d2fd79949cfc8a05b4c49/LICENSE) | [Python ≥3.11](https://github.com/ifccod/social-media-research-cli/blob/a1b13c9a86b1a0ea4d9d2fd79949cfc8a05b4c49/pyproject.toml)；E2，五站部分 client/签名边界 | 目标 ID 对应检查为切片候选；不接 browser bridge、硬页数上限或缺值补 0 |
| R15 [zhihulite/zhihu_zse96 · readme.md](https://github.com/zhihulite/zhihu_zse96/blob/b674e1f087c3acac58d3fd3c1b157d319541e78a/readme.md) | 2026-01-24 | [MIT LICENSE](https://github.com/zhihulite/zhihu_zse96/blob/b674e1f087c3acac58d3fd3c1b157d319541e78a/LICENSE) 与 README“不商用”文字不一致，需核清，不作法律裁决 | [Python ≥3.12](https://github.com/zhihulite/zhihu_zse96/blob/b674e1f087c3acac58d3fd3c1b157d319541e78a/readme.md)；Node ≥22/Lua ≥5.4/Dart ≥3.6.1；E4，仅 README/许可 | App x-zse96，不是 Web execjs 替代；不采用 |
| R16 [cv-cat/ZhihuApis · zhihu_apis.py](https://github.com/cv-cat/ZhihuApis/blob/0c5ce8e2a5f9d7108d34a525a6fb782309818652/apis/zhihu_apis.py) | 2026-08-18 | 所列树未见许可文件，复制授权待核 | [README：Python ≥3.10、Node ≥20](https://github.com/cv-cat/ZhihuApis/blob/0c5ce8e2a5f9d7108d34a525a6fb782309818652/README.md)；E2，局部 API/utils | 所读评论等接口不补足正式正文/作者门禁；不收编、不换签名 |
| R17 [DIYgod/RSSHub · zhihu/answers.ts](https://github.com/DIYgod/RSSHub/blob/17b9b9ff5fad12481bb5ea1217f6f1476f7dbde7/lib/routes/zhihu/answers.ts) | 2026-09-27 | [AGPL-3.0 LICENSE](https://github.com/DIYgod/RSSHub/blob/17b9b9ff5fad12481bb5ea1217f6f1476f7dbde7/LICENSE)，切片许可链未核 | [TypeScript](https://github.com/DIYgod/RSSHub/blob/17b9b9ff5fad12481bb5ea1217f6f1476f7dbde7/package.json)；Node ^22.22.2 或 ^24.15.0；E2，知乎 route/sign/browser、微博 keyword/utils | routes/sign/parser 职责方法借鉴；不接服务、全局 Cookie 或新浏览器会话 |

### B站

| 编号/候选与审阅入口 | push 日期 | 许可证据及疑点 | 语言/检查程度 | 能力与采用级别 |
|---|---|---|---|---|
| R18 [Nemo2011/bilibili-api · README](https://github.com/Nemo2011/bilibili-api/blob/3798d3b3bd3c3a93678d5a0367637a19262303ef/README.md) | 2026-07-06 | 当前树仅 README，历史许可不外推 | [已归档](https://github.com/Nemo2011/bilibili-api/blob/3798d3b3bd3c3a93678d5a0367637a19262303ef/README.md)；E4，没有此 SHA 的 SDK 实现 | 完全排除新 SDK 依赖建议，不据旧印象恢复能力清单 |
| R19 [BACNext/BACNext · README](https://github.com/BACNext/BACNext/blob/d04f2d5bcf9eabd366a1d46c156b44beb92cdb27/README.md) | 2026-08-17 | [MIT LICENSE](https://github.com/BACNext/BACNext/blob/d04f2d5bcf9eabd366a1d46c156b44beb92cdb27/LICENSE) | [API 文档项目，运行语言/版本未确认](https://github.com/BACNext/BACNext/blob/d04f2d5bcf9eabd366a1d46c156b44beb92cdb27/README.md)；E4，仅 README/许可 | 仅保留候选背景；没有审到 article 实现，更未验证端点，不替换本地 article |

R14 的 B站部分主要是视频能力，不能用跨站覆盖宣传替代 S03/M01—M04 的 article 路径。

### 相似采集工程、归档与测试库

| 编号/候选与审阅入口 | push 日期 | 许可证据及疑点 | 语言/检查程度 | 能力与采用级别 |
|---|---|---|---|---|
| R20 [auto-archiver · orchestrator.py](https://github.com/bellingcat/auto-archiver/blob/37917e2e56b92cb7243d21d92ec93b6e44258461/src/auto_archiver/core/orchestrator.py) | 2026-09-10 | [MIT LICENSE](https://github.com/bellingcat/auto-archiver/blob/37917e2e56b92cb7243d21d92ec93b6e44258461/LICENSE) | [Python ≥3.10、<3.13](https://github.com/bellingcat/auto-archiver/blob/37917e2e56b92cb7243d21d92ec93b6e44258461/pyproject.toml)；E2，extractor/enricher/media/metadata/storage | 模块职责及 manifest 方法借鉴；不接下载/云存储与自动 setup |
| R21 [Scrapy · files.py](https://github.com/scrapy/scrapy/blob/1084ff47fb4e4adbad163459430901e0f336e567/scrapy/pipelines/files.py) | 2026-09-27 | [BSD-3-Clause LICENSE](https://github.com/scrapy/scrapy/blob/1084ff47fb4e4adbad163459430901e0f336e567/LICENSE) | [Python ≥3.10](https://github.com/scrapy/scrapy/blob/1084ff47fb4e4adbad163459430901e0f336e567/pyproject.toml)；E3，Spider/media pipeline | 提取/下载结果分工方法借鉴；不接 Twisted/Crawler 调度 |
| R22 [yt-dlp · common.py](https://github.com/yt-dlp/yt-dlp/blob/c7fb478d21e9e59524befbe23f7801bb267fb880/yt_dlp/extractor/common.py) | 2026-09-16 | [根 Unlicense](https://github.com/yt-dlp/yt-dlp/blob/c7fb478d21e9e59524befbe23f7801bb267fb880/LICENSE)；第三方许可链另核 | [Python ≥3.10](https://github.com/yt-dlp/yt-dlp/blob/c7fb478d21e9e59524befbe23f7801bb267fb880/pyproject.toml)；E3，InfoExtractor | 输出分型方法借鉴；不接视频、认证或重试框架 |
| R23 [Crawlee · request queue](https://github.com/apify/crawlee-python/blob/c6fba871dc9b9e7cde2b14fbc30b187dee771e85/src/crawlee/storages/_request_queue.py) | 2026-09-26 | [Apache-2.0 LICENSE](https://github.com/apify/crawlee-python/blob/c6fba871dc9b9e7cde2b14fbc30b187dee771e85/LICENSE) | [Python ≥3.10](https://github.com/apify/crawlee-python/blob/c6fba871dc9b9e7cde2b14fbc30b187dee771e85/pyproject.toml)；E3，queue/SessionPool | 队列职责方法借鉴；不接调度/会话池，不以队列空作耗尽 |
| R24 [news-please · from_html](https://github.com/fhamborg/news-please/blob/f899850be2bb0059da101c13502ec88af057f974/newsplease/__init__.py) | 2026-04-14 | [Apache-2.0 LICENSE.txt](https://github.com/fhamborg/news-please/blob/f899850be2bb0059da101c13502ec88af057f974/LICENSE.txt) | [setup.py 无 python_requires，classifiers 3.8—3.12 不等于兼容证明](https://github.com/fhamborg/news-please/blob/f899850be2bb0059da101c13502ec88af057f974/setup.py)；E3 | 启发式正文不能替权威平台字段；不采用 |
| R25 [Scoop · scoopToWACZ.js](https://github.com/harvard-lil/scoop/blob/1a5c5178ea6b3cfee79f988aeaf36c1e52b5cab8/exporters/scoopToWACZ.js) | 2026-09-25 | [MIT LICENSE](https://github.com/harvard-lil/scoop/blob/1a5c5178ea6b3cfee79f988aeaf36c1e52b5cab8/LICENSE) | [Node ≥22](https://github.com/harvard-lil/scoop/blob/1a5c5178ea6b3cfee79f988aeaf36c1e52b5cab8/package.json)；E3，导出/交换记录 | 仅元信息/摘要方法借鉴；归档构件待未来明确需求，不增加 HAR/WARC/WACZ |
| R26 [warcio · warcwriter.py](https://github.com/webrecorder/warcio/blob/2a797aa8c6d67e70966ce9d1a690208ab250fbe7/warcio/warcwriter.py) | 2026-06-10 | [Apache-2.0 LICENSE](https://github.com/webrecorder/warcio/blob/2a797aa8c6d67e70966ce9d1a690208ab250fbe7/LICENSE) 与 [NOTICE](https://github.com/webrecorder/warcio/blob/2a797aa8c6d67e70966ce9d1a690208ab250fbe7/NOTICE) | [setup.py 无 python_requires，classifiers 3.8—3.13](https://github.com/webrecorder/warcio/blob/2a797aa8c6d67e70966ce9d1a690208ab250fbe7/setup.py)；E3，writer/iterator | 仅未来明确 WARC 需求才评包依赖，本轮不采用 |
| R27 [Scrapling · parser.py](https://github.com/D4Vinci/Scrapling/blob/e0d4d7563207b70c2cb38487c4e0dcbe0e2f04ca/scrapling/parser.py) | 2026-09-26 | [BSD-3-Clause LICENSE](https://github.com/D4Vinci/Scrapling/blob/e0d4d7563207b70c2cb38487c4e0dcbe0e2f04ca/LICENSE) | [Python ≥3.10](https://github.com/D4Vinci/Scrapling/blob/e0d4d7563207b70c2cb38487c4e0dcbe0e2f04ca/pyproject.toml)；E3，Selector | 保留本地既有辅助路径/锁定版；不因新 HEAD 升级或推广正式链 |
| R28 [Import Linter · forbidden.py](https://github.com/seddonym/import-linter/blob/31927f1457e3df673912cb5efb0afa6dbc37585f/src/importlinter/contracts/forbidden.py) | 2026-09-16 | [BSD-2-Clause LICENSE](https://github.com/seddonym/import-linter/blob/31927f1457e3df673912cb5efb0afa6dbc37585f/LICENSE) | [Python ≥3.10](https://github.com/seddonym/import-linter/blob/31927f1457e3df673912cb5efb0afa6dbc37585f/pyproject.toml)；E2，forbidden/independence | 首选新 dev 包候选，避免自研依赖图；只管静态 import |
| R29 [RESPX · router.py](https://github.com/lundberg/respx/blob/57d8c29705fdbbaeb5cd216f1ea3bb0386d7ba16/respx/router.py) | 2026-07-21 | [BSD-3-Clause LICENSE.md](https://github.com/lundberg/respx/blob/57d8c29705fdbbaeb5cd216f1ea3bb0386d7ba16/LICENSE.md) | [Python ≥3.8、HTTPX ≥0.25](https://github.com/lundberg/respx/blob/57d8c29705fdbbaeb5cd216f1ea3bb0386d7ba16/setup.py)；E2，router/transports | 复杂 HTTPX 匹配时的 dev 包候选；先用内建 MockTransport |
| R30 [VCR.py · config.py](https://github.com/kevin1024/vcrpy/blob/c599974b31f3e510df9b98e61513fe6889a50db0/vcr/config.py) | 2026-09-15 | [MIT LICENSE.txt](https://github.com/kevin1024/vcrpy/blob/c599974b31f3e510df9b98e61513fe6889a50db0/LICENSE.txt) | [Python ≥3.10](https://github.com/kevin1024/vcrpy/blob/c599974b31f3e510df9b98e61513fe6889a50db0/pyproject.toml)；E2，config/cassette/filters | 次选离线 HTTP cassette dev 包候选；不覆盖浏览器、不录原始交换 |

## S25｜接口拆解、项目落点与契约测试

下列测试均是后续采用的验收要求，v0.3 研究时没有运行。每组只把有证据的接口及方法落到既有边界；
包名、类名或相似职责不构成迁移理由，现有实现已经满足时只补对照记录，不重写。

### T01｜R01：保留 xhshow 签名接口，分离实验性会话状态

来源：[client.py 的 Xhshow.sign_headers/build_json_body](https://github.com/Cloxl/xhshow/blob/b242a84f9c5a6a971bc4c90c63ad43345a4bf8a1/src/xhshow/client.py)、
[session.py 的 SessionManager.get_current_state](https://github.com/Cloxl/xhshow/blob/b242a84f9c5a6a971bc4c90c63ad43345a4bf8a1/src/xhshow/session.py)。
可借部分：继续让现有 signer 接口输出签名头，client 负责发送；JSON body 的确定表示与签名输入成对核验。
这些所读签名接口不发送 HTTP；直接依赖 pycryptodome，内部加密/校验与安装资源仍非全闭包审计。
`get_current_state` 会更新内存计数，缺省时间/随机数也影响结果，不能视为无状态纯函数。
[README 会话段](https://github.com/Cloxl/xhshow/blob/b242a84f9c5a6a971bc4c90c63ad43345a4bf8a1/README.md)明确标为实验性、效果待验证；不让其接管 XHS 租约、登录、跨轮会话或 BrowserContext。
落点是既有 `sign_with_xhshow` 及平台 signer 接缝，保留当前锁定版；研究 SHA 不等于本地安装版本。
契约测试：相同受控时钟/随机输入和原始 body 得到旧新相同签名输入/输出；缺必要参数失败，无新增网络或持久状态。
[test_public_api.py](https://github.com/Cloxl/xhshow/blob/b242a84f9c5a6a971bc4c90c63ad43345a4bf8a1/tests/test_public_api.py)只提供格式/参数断言的参考，不能证明端点仍接受签名。

### T02｜R02/R04：可注入签名，不收编独立客户端与 Cookie 生命周期

来源：[ReaJason XhsClient.__init__/_pre_headers/request/get_note_by_id_from_html/get_user_info](https://github.com/ReaJason/xhs/blob/f4b62d9f8e4078e631fc6e4ec8e430bc711ee9f0/xhs/core.py)。
可借 sign 注入和详情/作者提取的分界；小段解析只有在现有 helper 确有缺口时才评估收编。
构造器自己创建 requests.Session、改 headers/Cookie；默认 timeout=10，HTML 路径没有沿同一代理/超时/错误包装。
返回混合 Response、dict、bool 或空值；默认混合内容搜索不代表仅图文，粉丝缺来源/observed 语义。
requests/lxml 与内部 helper 闭包待核，不能把整个 client 包装一层便当成纯 parser。
[test_xhs.py](https://github.com/ReaJason/xhs/blob/f4b62d9f8e4078e631fc6e4ec8e430bc711ee9f0/tests/test_xhs.py)含联网/写操作和旧参数调用，只读断言，不直接复制运行。
对照 [R04 _merge_response_cookies/_request_with_retry](https://github.com/jackwener/xiaohongshu-cli/blob/4d63f3c0c85ccd9054fa8e96d7f761aaf2507449/xhs_cli/client.py)：独立 HTTPX、默认 30 秒与 3 次尝试，修改 Cookie；
[Cookie 测试](https://github.com/jackwener/xiaohongshu-cli/blob/4d63f3c0c85ccd9054fa8e96d7f761aaf2507449/tests/test_cookies.py)涉及跨调用缓存，不能扩展为本项目跨轮缓存。
落点仅 XHS parser/client 的窄能力边界，不接 browser-cookie3/camoufox、跨轮 Cookie 或发布能力；R04 许可还未核清。
契约测试：空 items、非 JSON、缺粉丝、返回类型不符各自失败，不增加额外重试。
不新增独立会话管理体系或额外 BrowserContext；原 HTTP 客户端创建/关闭时机保持，不新增跨轮 Cookie 持久化。
单轮单 Context 与无跨轮登录态仅限 XHS；其它通用平台既有 Cookie 策略不变。

### T03｜R08：EndpointTable、RequestSpec 与解析/风险信号分工

来源：[当前 src/dtk 的 DouyinAdapter](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/src/dtk/platforms/douyin/adapter.py)、
[EndpointTable/EndpointSpec/RequestSpec](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/src/dtk/platforms/base.py)及 [endpoint 表](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/src/dtk/platforms/douyin/endpoints.py)。
`build_request`、`EndpointSpec.build_params/to_request` 给出请求描述，`parse_content/parse_author/detect_risk_control`
分开处理结果与风险；`parse_author_list/parse_collection_detail` 抛 UnsupportedContent 的做法可借，不把缺能力伪装为空结果。
未知 endpoint 由 EndpointTable.__getitem__ 抛 InvalidParam，与不支持能力分开；这是当前 `src/dtk` 路径，搜索 URL 常量不证明搜索实现。
补读 [parser.py](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/src/dtk/platforms/douyin/parser.py)后，可确认 `parse_author_posts` 用 max_cursor，`parse_collection_posts` 用 cursor，
显式观察 has_more，继续分页却缺游标时失败；空详情/用户与合法空列表分开。风险码注释仍称待响应样本确认。
`Content.raw` 和 Author.avatar 会携带原载荷/头像，图集按 images 非空分类；models/common/transport 闭包仍未审全。
Python 3.12、wreq、数据库/服务依赖不适合直接引入；不接新版 runtime、identitypool 或其默认状态管理。
落点仅借 RequestSpec/endpoint/parser 的职责拆分及观测与 S13 错误映射；正式推进仍由 application 判断。
不移植外部“整个 platforms 包无 I/O”的限制；S04 的我方 platforms/client 仍执行既有请求，
纯度仅约束 parser 与请求描述部分，原签名/请求栈不变。
签名输入须与最终发送的 method/path/query 编码/body 字节一致，避免重复编码。
契约测试：缺/真/假 has_more、游标 0/缺失、两种端点游标错用、不支持能力/无效 endpoint/正常空/风控分开；粉丝 0 不补缺，raw 不序列化。

### T04｜R06：F2 字段过滤器与微博接口，切片须切断请求基类

来源：[PostDetailFilter.images/UserProfileFilter.follower_count](https://github.com/Johnserf-Seed/f2/blob/c2c52a4da0cfe0ce646cc836738d7f1aca1308f8/f2/apps/douyin/filter.py)、
[WeiboCrawler](https://github.com/Johnserf-Seed/f2/blob/c2c52a4da0cfe0ce646cc836738d7f1aca1308f8/f2/apps/weibo/crawler.py)和 [微博 filter](https://github.com/Johnserf-Seed/f2/blob/c2c52a4da0cfe0ce646cc836738d7f1aca1308f8/f2/apps/weibo/filter.py)。
可借字段路径及详情/profile 过滤职责，作为抖音/微博 parser 的小块切片候选，不整包替换。
图片选首个 URL 不提供权威来源或 manifest；作者输出含头像，缺值没有本项目“未观察”证明。
所读微博 client 有用户/单帖能力，没有现成关键词搜索；不能以支持微博推导覆盖正式搜索链。
httpx/curl_cffi、JSONPath、Cookie 库和内部请求基类形成闭包；补取 JSON parser 文件为零字节，不能宣称缺值实现已审清。
[model.py](https://github.com/Johnserf-Seed/f2/blob/c2c52a4da0cfe0ce646cc836738d7f1aca1308f8/f2/apps/douyin/model.py)的 token default_factory 在实例化时调用，不能误写成导入即联网，也不能忽略实例化副作用。
落点限字段投影，保留现有 client、会话和搜索；候选来自开发分支，发行版与维护可靠性还需核实。
契约测试：真实粉丝 0 与缺字段分开，未观察不补默认值；图片顺序/来源稳定，头像及同记录重复 URL 首次序列化前清除。

### T05｜R07/R03：下载器提供反例，不继承缺值、时间和媒体默认值

来源：[TikTokDownloader Extractor.safe_extract/__format_date/run](https://github.com/JoeanAmier/TikTokDownloader/blob/473c90ff70c663cfb69310fff2b8d5192f200661/src/extract/extractor.py)。
`safe_extract` 用真值判断，把真实 0 变为默认值；`__format_date` 的 localtime(data or None) 使缺时间可能变当前时间。
Extractor 依赖 Parameter、记录器、Retry、curl client，某些分支写记录或发送视频 Range 请求，并非纯解析器。
可借类型分派思路与负例；日志中的原对象也可能包含头像，不能直接纳入本项目 fixture。
来源：[XHS Explore.run](https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/source/application/explore.py)、
[Image.get_image_link](https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/source/application/image.py)与 [Html.request_url](https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/source/application/request.py)。
图片候选职责可对照，但缺值填 -1/未知、宿主时区、实况视频、CDN 重建，以及 Cookie 分支同步 curl/关闭 TLS 校验均不继承。
失败压成空串、混合视频可能归图集，不能满足本项目权威图片与仅图文规则；Manager/retry 仍缺闭包证据。
两者均 Python 3.12 且为 GPLv3 声明，当前只方法借鉴，不直接依赖，也不默认批准复制代码。
落点为 S10/V15 的反例集合：真实 0 保留、缺时间拒绝、仅平台原始时间可转 Asia/Shanghai。
契约测试：缺时间绝不变本轮时间；非权威改写 URL 拒绝；视频/实况零请求，TLS 和图片字节门禁不因借鉴放宽。

### T06｜R12：extractor/job 消息分工与微博图片枚举

来源：[WeiboExtractor.items/_extract_status/_pagination](https://github.com/mikf/gallery-dl/blob/8b9a56d6b1a53bc13a80894625fd58b24de3063a/gallery_dl/extractor/weibo.py)、
[Job.dispatch](https://github.com/mikf/gallery-dl/blob/8b9a56d6b1a53bc13a80894625fd58b24de3063a/gallery_dl/job.py)及 [Extractor 请求基类](https://github.com/mikf/gallery-dl/blob/8b9a56d6b1a53bc13a80894625fd58b24de3063a/gallery_dl/extractor/common.py)。
Directory/Url/Queue 分别表达组织、媒体和再分派，适合借鉴“帖子观测/图片候选/后续能力”的区分。
微博按图片 ID 枚举原图的路径可作当前权威图片提取对照；不照搬其消息作为本项目 wire schema。
默认 videos=True、livephoto=True、text=False 不符合范围和完整正文要求；原 status/user 也进入消息。
分页分支遇缺字段或空列表可能停止，不可把生成器结束签成 source_exhausted。
requests.Session、Cookie/缓存、环境代理和 job 调度有额外状态；此 SHA GPL-2.0-only，当前只方法借鉴。
落点为微博 parser 与图片候选协议；下载继续原 session，提交由 artifacts/db 门禁负责。
契约测试：图序与来源稳定，长文必须完整，消息无头像；缺分页字段失败，video/livephoto 零请求，迭代结束不自动耗尽。

### T07｜R09—R11：微博历史实现用于路径与负例对照

来源：[Weibo.get_long_weibo](https://github.com/dataabc/weibo-crawler/blob/a3bfe515e9886b84151c609debdc636cbb0e9730/weibo.py)、
[PageParser](https://github.com/dataabc/weiboSpider/blob/720d52a58aeff3bdafdc552b90443842ebb94ba7/weibo_spider/parser/page_parser.py)与 [SearchSpider.parse_weibo](https://github.com/dataabc/weibo-search/blob/b4535b71d36ae61d13ab0083a18a152914f14ba7/weibo/spiders/search.py)。
有用部分是长文补全与页面提取所处位置，供核对“列表摘要不是权威全文”；不认定旧端点有效。
已读分支涉及关闭 TLS、构造时请求、日期/分页停止或视频路径耦合，不能作为本项目默认行为。
三仓当前树未见许可文件，无法确认复制授权；本轮不依赖、不收编，概括方法不复制实现。
落点为微博完整正文与错误场景清单，不引入其 Scrapy 调度或另一个 crawler 主循环。
契约测试：摘要不得替正文，长文失败不变空正文，日期/页数边界不能签署耗尽，构造 parser 不产生网络。

### T08｜R13/R14/R15/R16：知乎 URL、载荷、ID 检查与签名范围

来源：[Kris extract_article_id/fetch_article_item/parse_article_metadata](https://github.com/KrisTHL181/zhihu-cli/blob/e8f14415519ef766e520f1e04a7ef3c7c0455ee3/src/zhihu_cli/content/handlers/article.py)、
[请求分派](https://github.com/KrisTHL181/zhihu-cli/blob/e8f14415519ef766e520f1e04a7ef3c7c0455ee3/src/zhihu_cli/content/handlers/request_tool.py)。
可借 URL、载荷与投影分离，但 URL 未约束域名，存在 content 键即通过、统计缺值补 0、作者字段不足。
默认 daemon 优先、失败直连及会话重建不接入；Python 3.12、curl-cffi/UI/MQTT 等闭包不适合整包。
来源：[ifccod ZhihuClient._detail/get_user](https://github.com/ifccod/social-media-research-cli/blob/a1b13c9a86b1a0ea4d9d2fd79949cfc8a05b4c49/reverse/zhihu_reverse/client.py)。
目标对象类型/ID 对应检查可作为小段切片候选；并不保证正文或作者完整，也不替代原 Web 签名。
client 修改会话头、访客初始化、JS/websocket/browser bridge 闭包未闭合；[微博 client](https://github.com/ifccod/social-media-research-cli/blob/a1b13c9a86b1a0ea4d9d2fd79949cfc8a05b4c49/reverse/weibo_reverse/client.py)的缺粉丝补 0、头像/视频与 100 页上限不复用。
R15 只有 README/许可，明确是 App 协议，不是当前 Web execjs 的等价实现；R16 局部接口及许可证据也不足以替换。
落点限知乎纯解析和响应身份校验，现有相同能力存在即不重写；浏览器、execjs 和维护责任不重开讨论。
契约测试：外域/错误对象类型/ID 错配/空正文/缺粉丝分别失败；真实发帖时间保真，不引额外会话、签名或页数停止。

### T09｜R17：RSSHub routes/sign/parser 的分离与局限

来源：[知乎 answers handler](https://github.com/DIYgod/RSSHub/blob/17b9b9ff5fad12481bb5ea1217f6f1476f7dbde7/lib/routes/zhihu/answers.ts)、
[getSignedHeaders](https://github.com/DIYgod/RSSHub/blob/17b9b9ff5fad12481bb5ea1217f6f1476f7dbde7/lib/routes/zhihu/sign.ts)和 [createBrowserClient](https://github.com/DIYgod/RSSHub/blob/17b9b9ff5fad12481bb5ea1217f6f1476f7dbde7/lib/routes/zhihu/browser.ts)。
可借 route 选任务、薄 sign 包装、parser/格式化分工；浏览器 client 校验 HTTP/JSON 与初始化阶段也可作错误分类对照。
answers 固定 limit=7 且直接读 data[0].author，没有完整分页和空数组保护；encrypt/withZhihuClient 等闭包未审全。
来源：[微博 keyword route](https://github.com/DIYgod/RSSHub/blob/17b9b9ff5fad12481bb5ea1217f6f1476f7dbde7/lib/routes/weibo/keyword.ts)与 [weiboUtils/formatExtended](https://github.com/DIYgod/RSSHub/blob/17b9b9ff5fad12481bb5ea1217f6f1476f7dbde7/lib/routes/weibo/utils.ts)。
cards 校验及无 posts 显式报错可借；访客场景 RenewWeiboCookiesError 不能转换成健康空页或耗尽。
tryWithCookies/getCookies 创建浏览器、缓存/更新 Cookie 并重试；formatExtended 改 pics、混入文章头图且仍输出 author.avatar。
HTML 输出不提供本项目 manifest/粉丝证据，允许 xhr/fetch 也不等于已阻断所有视频 URL。
Node/AGPL 框架仅作方法对照，不接服务、跨轮 Cookie 或浏览器生命周期，不改当前签名。
落点为平台请求/解析/错误边界。契约测试：缺 cards、空 posts、过期会话、非 JSON、空 answers 分别处理；头像首序列化清除，零新 Context。

### T10｜R05：feed_detail/page_ready 的页面身份与就绪证据

来源：[GetFeedDetailWithConfig/extractFeedDetail/checkPageAccessible](https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/xiaohongshu/feed_detail.go)、
[waitFeedPageReady](https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/xiaohongshu/page_ready.go)及 [Search/waitFeedsChanged](https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/xiaohongshu/search.go)。
可借状态容器、错误容器与 feedID 的对应检查，以及先验证筛选参数、传播调用方取消，并在 Context(ctx) 后重新设置局部 Timeout 的分工。
但就绪 8 秒超时仅 warn 后继续；错误容器查询/读文本失败视为可访问，筛选等待超时可能返回旧结果。
extractFeedDetail 先 JSON.stringify 整张 noteDetailMap 再选 ID，返回路径包含较宽的 state/评论；
该内存临时表示不能直接写入本项目 JSONL、日志或 fixture；本轮未观察到该载荷持久化，不据此宣称外部程序泄露。
采用时须保留识别头像重复 URL 所需的原字段上下文，所有项目持久化出口仍强制执行 S10 的首次序列化清理；URL 日志仍须清理 token。
单次读取 state、评论默认 20 条及尝试上限均不能证明正式来源耗尽。
Go/Rod/Gin/MCP/headless_browser 及评论采集、发布能力不接入，不读取跨轮 Cookie，不让其控制现有浏览器生命周期。
[feed_detail_test.go](https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/xiaohongshu/feed_detail_test.go)仅覆盖回复按钮文案、配置归一、max attempts，不能称就绪/取消/端点已验证。
落点为现有 XHS 页面提取对照；契约测试：state 迟到、错误容器、ID 错配、取消、旧筛选结果不得假成功/耗尽，日志无头像/token，单 Context 不变。

### T11｜R20：extractor/enricher/media/storage 分工不授予提交权

来源：[ArchivingOrchestrator.archive](https://github.com/bellingcat/auto-archiver/blob/37917e2e56b92cb7243d21d92ec93b6e44258461/src/auto_archiver/core/orchestrator.py)、
[Extractor](https://github.com/bellingcat/auto-archiver/blob/37917e2e56b92cb7243d21d92ec93b6e44258461/src/auto_archiver/core/extractor.py)、[Enricher](https://github.com/bellingcat/auto-archiver/blob/37917e2e56b92cb7243d21d92ec93b6e44258461/src/auto_archiver/core/enricher.py)、
[Media.store](https://github.com/bellingcat/auto-archiver/blob/37917e2e56b92cb7243d21d92ec93b6e44258461/src/auto_archiver/core/media.py)及 [BaseModule manifest 约定](https://github.com/bellingcat/auto-archiver/blob/37917e2e56b92cb7243d21d92ec93b6e44258461/src/auto_archiver/core/base_module.py)。
可借提取、补充元数据、媒体和存储的职责以及插件 manifest 声明思路，不据此新建完整插件框架。
setup 可联网/写配置，下载器用独立 requests 写临时文件，MIME 主要依文件名；缓存命中可直接返回。
首 extractor 成功只退出提取循环，之后仍有 enricher、媒体存储、formatter 和数据库通知，不等于已通过本项目门禁。
补读 [Metadata.merge/remove_duplicate_media_by_hash/choose_most_complete](https://github.com/bellingcat/auto-archiver/blob/37917e2e56b92cb7243d21d92ec93b6e44258461/src/auto_archiver/core/metadata.py)：
默认右覆盖、list 拼接、media.extend 不是来源优先级；缺文件只警告跳过，按媒体/元数据数量选完整度不等于权威字段齐备。
[Storage.store/upload](https://github.com/bellingcat/auto-archiver/blob/37917e2e56b92cb7243d21d92ec93b6e44258461/src/auto_archiver/core/storage.py)忽略 upload 返回 bool 后仍 add_url，不能作为跨文件/SQLite 原子提交实现。
`get_timestamp(utc=True)` 的 replace 不是时区转换；is_success 的状态字符串判断也不能替代正式门禁。
落点只映射到现有 application/records/artifacts/db；Python、云存储、浏览器/视频依赖不整包引入。
契约测试：缺文件、upload=false、部分存储失败不得登记完成；去重保留角色/来源，真实发布时间不变，ContentCommit/DiscoveryCommit 仍由我方所有者执行。

### T12｜R21/R22/R23：借职责与结果分型，不接框架调度

来源：[Scrapy FilesPipeline.get_media_requests/item_completed](https://github.com/scrapy/scrapy/blob/1084ff47fb4e4adbad163459430901e0f336e567/scrapy/pipelines/files.py)。
可借请求候选与下载结果分离；默认 90 天过期、成功项汇总与 Crawler/Twisted 依赖不满足完整整帖图片门禁。
落点是 artifacts 校验接口对照。契约测试：一张图失败也阻整帖完成，缓存命中仍要验证字节和来源。
来源：[yt-dlp InfoExtractor.extract/url_result](https://github.com/yt-dlp/yt-dlp/blob/c7fb478d21e9e59524befbe23f7801bb267fb880/yt_dlp/extractor/common.py)。
可借明确的输出分型；extract 还包含初始化、认证与重试，不能看作纯 parser，视频主流程不在范围。
落点是观测/继续分派的类型表达。契约测试：URL 分派只是待处理结果，不能当正文完整、下载成功或正式耗尽。
来源：[Crawlee RequestQueue.fetch_next_request/is_finished](https://github.com/apify/crawlee-python/blob/c6fba871dc9b9e7cde2b14fbc30b187dee771e85/src/crawlee/storages/_request_queue.py)、
[SessionPool](https://github.com/apify/crawlee-python/blob/c6fba871dc9b9e7cde2b14fbc30b187dee771e85/src/crawlee/sessions/_session_pool.py)。
可借“暂未取得任务/队列结束”分开，None 不等于完成；即使队列结束也不是平台耗尽。
落点是 S07 的观测/决策区分；不接 SessionPool、重调度或替换原进程形态。
契约测试：暂空、重试待办、队列完成均不得自动生成 adaptive_search_stopped(source_exhausted)。

### T13｜R24/R27：通用正文与选择器不提供平台权威性

来源：[NewsPlease.from_html](https://github.com/fhamborg/news-please/blob/f899850be2bb0059da101c13502ec88af057f974/newsplease/__init__.py)与 [ArticleExtractor](https://github.com/fhamborg/news-please/blob/f899850be2bb0059da101c13502ec88af057f974/newsplease/pipeline/extractor/article_extractor.py)。
新闻启发式提取可作反例；默认 fetch_images=True、空输入返回 {}，还引入新闻/数据库依赖，当前不采用。
落点为字段来源测试：正文“看起来完整”不代表平台权威详情，作者文本也不证明粉丝 observed。
契约测试：搜索摘要/启发式正文不能升级 detail_observed，空 HTML 不变健康空结果，不触发图片网络。
来源：[Scrapling Selector.css/xpath](https://github.com/D4Vinci/Scrapling/blob/e0d4d7563207b70c2cb38487c4e0dcbe0e2f04ca/scrapling/parser.py)。
成熟选择器能力可继续用现有库；adaptive 默认关闭，启用后涉及 SQLite，不能因解析复用扩大状态边界。
落点仍是既有辅助路径，正式平台当前 Parsel 不被顺手替换，Patchright 也不推广。
契约测试：辅助路径关闭时不创建其状态/网络；解析缺值和 Selector 异常按项目错误映射，不跨界传对象。

### T14｜R29：优先 HTTPX 内建 MockTransport，复杂匹配才用 RESPX

来源：[Router.resolve/aresolve/handler/async_handler](https://github.com/lundberg/respx/blob/57d8c29705fdbbaeb5cd216f1ea3bb0386d7ba16/respx/router.py)及 [transports.py](https://github.com/lundberg/respx/blob/57d8c29705fdbbaeb5cd216f1ea3bb0386d7ba16/respx/transports.py)。
MockTransport 的处理函数足以模拟固定请求时不加库；多路匹配、调用断言和响应序列再评 RESPX 为 dev 依赖。
可让业务 client 保持既有会话所有权；不为了 mock 改正式 transport，更不将 B站 urllib 换成 HTTPX。
RESPX 默认严格匹配，但关闭 assert_all_mocked 会返回空 200，pass-through 会放行；二者均禁止用于离线差分。
包声明 HTTPX≥0.25，仍须与当前锁兼容核对；mock 调用历史可能持有请求秘密，只用合成/净化输入。
落点为平台请求边界测试；只覆盖 HTTPX，其他网络栈与浏览器须独立失败关闭。
契约测试：未注册请求立即失败；比较方法、URL、body 字节和签名输入，模拟非 JSON、超时、0/缺值并校验总重试预算。

### T15｜R30：VCR.py 只作受控离线 cassette 候选

来源：[VCR.use_cassette/get_merged_config](https://github.com/kevin1024/vcrpy/blob/c599974b31f3e510df9b98e61513fe6889a50db0/vcr/config.py)、
[Cassette.append/play_response](https://github.com/kevin1024/vcrpy/blob/c599974b31f3e510df9b98e61513fe6889a50db0/vcr/cassette.py)及 [filters.py](https://github.com/kevin1024/vcrpy/blob/c599974b31f3e510df9b98e61513fe6889a50db0/vcr/filters.py)。
适合已有多请求 HTTP fixture 的离线重放，但优先级低于内建 mock/RESPX；不因库可录制而建设新采集系统。
VCR 默认 ONCE、无自动脱敏，默认 match_on 不含 body；append 在响应过滤前就可能输出日志。
只允许已净化或合成 cassette，record_mode=none，显式匹配 body/必要 headers；缺匹配不得回真实网络。
PyYAML/wrapt 及对 HTTP 客户端的 monkeypatch 是开发依赖闭包，不能让补丁泄漏到其他测试或正式运行。
落点是离线 HTTP 契约验证，不是浏览器回放；不覆盖浏览器全部请求，不增加 raw/HAR/WARC 归档。
契约测试：cassette 缺项、body 不同立即失败，文件不自动增长，stdout/stderr/日志也无 Cookie、token、头像及重复 URL。

### T16｜R28：Import Linter 直接做开发依赖候选

来源：[ForbiddenContract.check](https://github.com/seddonym/import-linter/blob/31927f1457e3df673912cb5efb0afa6dbc37585f/src/importlinter/contracts/forbidden.py)与
[IndependenceContract.check](https://github.com/seddonym/import-linter/blob/31927f1457e3df673912cb5efb0afa6dbc37585f/src/importlinter/contracts/independence.py)。
可直接表达 S05 的禁止依赖和平台独立性，默认含间接链；不自研另一套 import 图扫描器。
Grimp/Click/Rich 等开发闭包及版本兼容仍需核验；静态图不涵盖动态 import、资源读取、运行期权限或生命周期。
规则应针对模块职责，不粗暴禁止 Pydantic/标准库；特例逐条记录原因与退出门禁，避免宽泛 ignore 隐藏反向依赖。
落点为未来 dev 检查，按实际包布局配置，不因草案中示意层名提前建目录。
契约测试：加入平台互相导入、records 依赖请求实现或内部反向 import scripts 时应失败；合法自有模型/标准库使用不误报。

### T17｜R25/R26：归档构件只留给未来明确需求

来源：[scoopToWACZ](https://github.com/harvard-lil/scoop/blob/1a5c5178ea6b3cfee79f988aeaf36c1e52b5cab8/exporters/scoopToWACZ.js)、
[ScoopProxyExchange](https://github.com/harvard-lil/scoop/blob/1a5c5178ea6b3cfee79f988aeaf36c1e52b5cab8/exchanges/ScoopProxyExchange.js)、
[WARCWriter.write_record](https://github.com/webrecorder/warcio/blob/2a797aa8c6d67e70966ce9d1a690208ab250fbe7/warcio/warcwriter.py)与 [ArchiveIterator](https://github.com/webrecorder/warcio/blob/2a797aa8c6d67e70966ce9d1a690208ab250fbe7/warcio/archiveiterator.py)。
可借元信息、摘要和归档职责分离的思路；本轮既不新增 schema，也不增加原始 HTTP 响应留存。
Scoop 的 Node≥22、自带 Chromium/postinstall、临时文件及原始交换记录闭包不适合替代 CfT。
warcio 主要依赖 six，但默认不验摘要，也不提供头像清理、图片角色或 SQLite 事务保证。
仅未来用户明确需要归档时，再评成熟 writer 包及许可链；当前落点只是设计边界说明。
未来契约测试先验证敏感字段与头像清理、摘要校验和故障处理；当前验收应确认未新增 raw/HAR/WARC/WACZ 产物要求。

## S26｜具体复用清单与构件分工

以下区分本地静态调用事实与外部选型推断。现有依赖声明见[根 pyproject](../pyproject.toml)和
[fork pyproject](../tools/MediaCrawler/pyproject.toml)；v0.3 研究时未运行这些调用，不重新验证“现有可用”的用户基线。
保留是保留本地既有锁定版本和有效行为，不把 S24 的新 HEAD 写入依赖或 lock。

| 构件 | 已定位本地调用 | 拟保留职责与边界 |
|---|---|---|
| xhshow | [XHS playwright_sign.py](../tools/MediaCrawler/media_platform/xhs/playwright_sign.py)：51；[client.py](../tools/MediaCrawler/media_platform/xhs/client.py)：152 | 继续签名接口；不顺带启用实验性会话管理 |
| HTTPX | [httpx_util.py](../tools/MediaCrawler/tools/httpx_util.py)：6；[XHS client.py](../tools/MediaCrawler/media_platform/xhs/client.py)：191 | 原请求工厂/transport，保留 headers、代理、超时和序列化输入 |
| Parsel | [知乎 help.py](../tools/MediaCrawler/media_platform/zhihu/help.py)：132、155 | Selector/图片解析；外部 Selector 留在 parser 内 |
| Pydantic | [m_zhihu.py](../tools/MediaCrawler/model/m_zhihu.py)：25；[help.py](../tools/MediaCrawler/media_platform/zhihu/help.py)：326 | 继续模型校验；项目拥有 schema 的模型可作契约，外部模型不直接当项目 wire |
| Pillow | [image_materialization.py](../src/trippostcollect/artifacts/image_materialization.py)：141、156、160 | 图片打开/verify 与项目错误映射，不自写图片解码器 |
| Tenacity | [XHS client](../tools/MediaCrawler/media_platform/xhs/client.py)：168；[微博 client](../tools/MediaCrawler/media_platform/weibo/client.py)：104；[知乎 client](../tools/MediaCrawler/media_platform/zhihu/client.py)：90 | 保留各自次数/等待/传播，不叠一层全额重试 |
| Playwright | [mediacrawler_crawl.py](../scripts/mediacrawler_crawl.py)：1011 | 原 CfT/浏览器行为、登录和借用能力，不重包全部 API |
| pyexecjs/execjs | [知乎 help.py](../tools/MediaCrawler/media_platform/zhihu/help.py)：253；[抖音 client.py](../tools/MediaCrawler/media_platform/douyin/client.py)：388 | 既有 JS 桥和资源；抖音所读接入排除 general/search，不外推所有请求 |
| 标准库 urllib | [mediacrawler_crawl.py](../scripts/mediacrawler_crawl.py)：4970 | B站 article 原 HTTP 链，不能为统一请求栈改成 HTTPX |
| Scrapling/Patchright | [ctf_scrapling_preflight.py](../scripts/ctf_scrapling_preflight.py)：217；[ctf_resource_crawl.py](../scripts/ctf_resource_crawl.py)：794 | 既有辅助路径原样保留，不据此称五站正式路径使用它们 |
| requests/lxml | 现有 requests 声明、Parsel 等依赖线索 | 存活 requests 调用、lxml 传递关系/独立调用未核；不承诺删除或无条件保留全部闭包 |

上述有调用证据的构件不是仅测试依赖；声明位置本身也不能证明生产调用或包可删除。
通用 hashlib、sqlite3、路径/原子替换等继续标准库；已有项目原子写入与目录同步协议保持，
不新造基础设施，也不以一个通用 wrapper 抹平文件和数据库提交不确定性。

| 优先次序/方式 | 具体建议 | 获准实施后才需补的证据 |
|---|---|---|
| 1 保留本地 | 上表构件与本地 fork 已有签名/请求/解析/下载能力 | S15 存活闭包与 S18 旧新 fixture 差分；不自动升级 |
| 2 新 dev 包候选 | Import Linter（R28）；HTTPX 内建 MockTransport 优先，按需 RESPX（R29） | Python 3.11/当前锁兼容、静态图边界、未匹配请求失败关闭 |
| 3 条件 dev 候选 | VCR.py（R30），仅现有合成/净化 cassette 确有价值时 | record_mode=none、body 匹配、日志清理及其他网络栈阻断 |
| 4 小切片候选 | F2 字段路径（R06）、ReaJason 纯解析（R02）、Kris URL/载荷（R13）、ifccod ID 检查（R14） | 真实缺口、精确文件/符号许可链、最小闭包、缺值/时间/头像负例；有现成能力就不重写 |
| 5 方法借鉴 | R05/R07/R08/R09—R12/R17/R20—R23 等职责与反例 | 不复制未核许可代码，不搬调度、会话池、服务或平台默认循环 |
| 当前不采用 | R15/R16 的签名替换、R18 新 SDK、R19 article 替代、R24 启发式权威正文 | 缺口或不适配已明确；不能因近期 push 反转判断 |
| 仅未来归档需求 | R25/R26 | 单独明确需求与评审，不改变本轮 schema/原始响应禁止留存边界 |

“小切片”不是绕过许可、测试或 Python 约束的办法；R13 整包要求 3.12，切片也须确认语法、
标准库 API、数据模型、间接 helper 和运行时闭包在 3.11 可用。未确认时只保留方法描述。
R03/R04/R07/R08/R13 等整包排除直接接入，不代表其全部方法不可研究；具体级别以 S24/T01—T17 为准。
S24 是研究覆盖表，不是给依赖管理器的输入，不会把 30 个候选加入项目。

以下本项目语义仍由项目拥有，它们不是对成熟基础库的重复实现：

- XHS 精确租约、单轮临时 profile/单 Context、信号收束与批次 ACK，见 S09/S12。
- `source_exhausted` 的停止证据、scope、已处理候选、前沿推进批准与正式完成裁决，见 S02/S07/S13。
- 权威正文/作者/粉丝来源、真实发帖时间、头像键及同记录重复 URL 清理，见 S06/S10。
- staging 复验、文件晋升、SQLite 提交确定性，以及 ContentCommit 与 DiscoveryCommit 的权限，见 S11/S12。

现有本地实现证据还包括 [sanitization.py](../src/trippostcollect/records/sanitization.py)：146、209，
[batch_checkpoint.py](../src/trippostcollect/xhs/batch_checkpoint.py)：264、[leases.py](../src/trippostcollect/xhs/leases.py)：974
及 [terminal.py](../src/trippostcollect/xhs/terminal.py)：180。v0.3 这里只定位职责，没有运行故障验证。
模块化通过窄接口连接这些语义与成熟库，保留平台流程；不把共享 schema 变成五站统一大循环。

## S27｜每次采用的决策记录与未决证据

本节是未来评审模板，不是本轮待执行命令。每个实际拟采用包或代码切片应有独立记录，
在记录齐备、相应范围获准且 S20 治理条件满足之前，不把建议写成正式依赖或完成状态。

| 必填项 | 需要的具体证据/判定 |
|---|---|
| 类型与真实缺口 | 包依赖/切片收编/方法借鉴；缺的是哪个字段、接口或检查；为何现有库/本地 fork 不能满足 |
| 固定来源 | owner/repo、完整 source SHA、文件、符号、永久链接；发行包版本与源码关系，不跟浮动 HEAD |
| 许可与来源链 | LICENSE 原文、版权头、适用 NOTICE、复制/修改标记、切片内外来代码许可链；声明冲突未核清则不采用 |
| 最小闭包 | 切片依赖的 helper/model/常量/资源/异常/初始化、直接与传递包、动态 import；不能只数 import 行 |
| 版本与运行环境 | 当前锁定版及拟选版、Python 3.11、Node/execjs/原 driver 约束；声明兼容与实际验证分列 |
| 运行副作用 | import/构造/调用/关闭分别是否联网、读凭证、改全局状态、写文件/SQLite、启浏览器、缓存或启动后台任务 |
| 替换接缝与所有权 | 替代哪个现有符号，输入输出、错误映射、会话/资源所有者；不增加跨平台直接 import 或提交权 |
| HTTP 与预算 | 方法/URL/query/body 编码、签名输入、超时与各层重试包含关系；最大尝试与墙钟预算不能相乘 |
| 旧新 fixture 差分 | 同一净化/合成输入的稳定字段、缺/0/空、平台时间、来源、图序、事件、错误、前沿；差异逐项解释 |
| 禁意外网络 | 未匹配请求 fail-closed；禁 pass-through、空 200、自动录制与真实端点兜底；列出未被 mock 覆盖的栈 |
| 维护证据 | 关键模块实际改动、问题响应/修复内容、版本发布关系；push 日期、机器人或文档提交不能替代 |
| 回退与删除 | 单接缝回退点、旧产物/状态可读、提交不确定处置、过渡层退出；不得删已 ACK 快照或手改 checkpoint |
| 授权与验收 | 采用范围、治理窗口、适用 V/T 项及证据路径；未运行项明示，正式试跑须另批，不以数量停止冒充完成 |

证据应分栏保存为“外部观察”“本地静态接缝”“本地验证结果”。外部测试文件存在、README 的能力
声明或包支持 Python 3.11 都不能填进“本地已通过”；v0.3 该栏只有文档级校验，没有运行能力验证。
原复评检测根 `.venv` 为 Python 3.12.13、fork `.venv` 为 Python 3.11.15，当时隔离测试及失败保留。
当前 main 已在独立锁定环境通过 Python 3.11/3.12 全部 lane 与所选 fork 测试，#1/#2 不再未决；
这不证明尚未完成的迁移目标依赖、JS、资源、辅助入口完整闭包兼容通过。具体版本及覆盖边界见 P00，当前复用 [testing.md](testing.md)。
直接解释器检测不保证未来 `uv run python` 解析恒定，也不能由根环境缺包推断 worker 缺依赖。
对签名库不复制大段算法作“研究成果”；保留来源和接口契约即可，算法更换并非已批准事项。
对日志/HTTP fixture 同样执行头像与秘密清理，不能为了可重放而放宽 S10 的首次序列化要求。

当前工程缺口及关闭位置：

| 缺口 | 已知边界 | 未来关闭位置 |
|---|---|---|
| 本地存活依赖完整性 | requests/lxml、动态加载、安装资源和辅助入口尚未闭包；旧 fork 不是可直接整树删除的死代码 | P00/P05、V13、S15—S16 |
| 候选许可链 | dataabc 三仓、ZhihuApis 未确认复制授权；R04 缺独立 LICENSE；R15 MIT/README 表述不一致；GPL/第三方切片另核 | 各采用记录；未核清不收编 |
| Python/版本 | R03/R07/R08/R13/R15 新快照要求 3.12；包声明、开发分支和本地锁不能混为一谈 | 包/切片评审及获准后的安装验证 |
| 解析与传输 | R06 缺值 helper、R08 models/transport、R13/R14 session/bridge 等未完整核查；v0.3 研究无端点实测 | V15/V16、对应 T 项；不先替换 |
| 完成与提交 | 外部 iterator/queue/archive 成功均不证明本项目耗尽、字段或内容事务成功 | V01/V07/V08/V11、S12 |
| 平台差分与故障 | 旧新 fixture、超时/重试、ACK 崩溃窗口、文件/SQLite 不确定性在 v0.3 研究时均未执行 | S18 的适用矩阵及 P00 当前证据，不用静态结论补签 |
| 实施准备 | D06/D10/D12 已确认，首期五站完整交付；D14 两 issue 已解决，但迁移闭包、精确接口与读取时点未齐；D11/D13 技术判断由助手负责，平台重构尚未授权 | S21、P00；不重开外部选型，不把主线全绿当 P00 ready 或实施授权 |

采用评审最后与 S22 交叉核验：项目自有 Pydantic 可用；B站 urllib 未换栈；辅助驱动未推广；
五站规则与结果统一而流程可不同；XHS ACK 仍可早于最终 ContentCommit，DiscoveryCommit 独立，
XHS 单 Context、无跨轮登录态不变；其它通用平台现有登录态策略不变；无 raw 归档不变。
S02 仍是唯一真源，S20 仍控制治理切换。
任何工具或候选若要求破坏这些边界，应保留现有实现或另行评审真实需求，不能通过“复用优先”自动豁免。
本稿为**讨论稿 v0.5，未批准实施**；研究与保护性测试准备不表示迁移或端点验收完成。
