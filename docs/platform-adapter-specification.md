# 平台适配详细迁移规格 v0.7

> 2026-09-28 完成 P00-08 静态复核并补齐全量符号账；设计就绪 D 门禁的静态部分已满足，实施另需授权。
> 本文中的目标文件、类型和接口尚未实现。主设计保留原则与原研究，本文是目录、接口、读取时点和任务卡的唯一详细定义；
> 逐符号处置见[附录 C8 全量符号账](platform-adapter-symbol-ledger.md)，执行清单以 GitHub issues 跟踪（见 G）。
> 当前运行仍遵循[正式契约](formal-crawl-contract.md)。T14 已执行（#19）：fork 子模块 `tools/MediaCrawler`、私有桥 E、`platforms/_fork_bridge.py` 与 `scripts/execution_state.py` 过渡模块均已删除；下文 F 事实中的 M/E/ES 路径和行号只作历史锚点，不再对应仓库文件。

## A｜固定基线与证据口径

| 标识 | 固定值／含义 |
|---|---|
| 根源码 F | `fe3e28ac7cc9575968e3279dd0e1c60ad0b7b1c1`，分支 `chore/platform-adapter-preflight`；本次补审起始已有三份跟踪文档修改及本规格未跟踪文件，保留原编辑 |
| 已修复主线 | `b7e52db254530d2dcd7657a0560b0103e4ddb256`；#1/#2 已关闭，既有 CI 只证明旧实现 |
| fork M | `tools/MediaCrawler` @ `2bcde689cc9a1b50cbcc7255597e9da1ff1a1b30` |
| F 事实 | 下表旧路径、符号、行号来自固定源码的定向静态审计；不是动态运行或端点验收 |
| T 目标 | 新位置、签名、任务和构建策略是本次选定方案；代码路径用代码文本表示，不伪装成已存在链接 |
| 上游对照 U↑ | 本地只读副本 `../MediaCrawler-upstream` @ `380b426000aac3d612837ed72c99808347dc94c9`（NanmiCoder/MediaCrawler，2026-09-19）；与 fork 分叉点 `d6f7c5bb906b6dac40ddf343ef9e26438a3de092`，fork 其后 102 个本地提交、上游其后 11 个提交 |
| 审计输入 | v0.6 三份平台/XHS/边界静态审计的关键结论已收入正文；v0.7 对全部锚点、CLI、env 与 1234 个定义做了程序化核对，见附录 C8 |
| 本轮验证 | 文档 diff、相对链接、符号行锚点、D2 CLI 默认值、env 名称双向对照、AST 全量枚举及冻结校验；未执行项目 import、测试、JS、安装、浏览器或联网 |

fork 与上游的文件级关系（以分叉点为准）决定哪些代码是本地改造、哪些仍是上游原样：
`search_safety.py`、`manual_wait.py`、`image_manifest.py`、`image_download_retry.py`、`trippostcollect_adaptive.py`、
`trippostcollect_behavior.py`、`zhihu_store_media.py` 为 fork 新增；四站 core/client、四站 store 投影、`cdp_browser.py`、
`browser_launcher.py`、XHS login、知乎 help 为 fork 大幅改造；`help.py`（抖音/小红书/微博）、四站 login（除 XHS）、field、
`libs/*.js`、`crawler_util.py`、`time_util.py`、`slider_util.py`、`easing.py`、`cache/*`、`proxy/*` 与分叉点完全相同。
上游其后的媒体下载重构（`media_platform/*/media.py`）与本项目 staging/manifest 路径冲突，不采用；抖音风控头见 H/R06。
`LICENSE` 与上游逐字节相同。

所有旧锚点按 F/M 解释。例：`C:run_bilibili_article_search:5020` 即根基线的
`scripts/mediacrawler_crawl.py` 第 5020 行定义；某些行号标调用位置而非定义时明确写“调用”。
原主稿 S18、S23—S27 的历史证据不改写为当前结果；本规格不重开 30 候选、17 组研究。

| 缩写 | 相对仓库根的旧路径 |
|---|---|
| C / E / R / X | `scripts/mediacrawler_crawl.py` / `scripts/mediacrawler_export_entrypoint.py` / `scripts/crawl_runner.py` / `scripts/xhs_runner.py` |
| W / Q / B | `scripts/mediacrawler_login_warmup.py` / `scripts/repair_xhs_posts.py` / `scripts/repair_bilibili_articles.py` |
| U / H / HF / ES | `M/tools/trippostcollect_adaptive.py` / `scripts/mediacrawler_behavior.py` / `scripts/human_flow.py` / `scripts/execution_state.py` |
| WB / DY / ZH / XK | `M/media_platform/weibo` / `M/media_platform/douyin` / `M/media_platform/zhihu` / `M/media_platform/xhs` |
| SW / SD / SZ / SX | `M/store/weibo` / `M/store/douyin` / `M/store/zhihu` / `M/store/xhs` |
| BC / XD / XL / XT | `src/trippostcollect/xhs/batch_checkpoint.py` / `discovery.py` / `leases.py` / `terminal.py`（后三者同一 xhs 目录） |
| P / A / RT | **目标** `src/trippostcollect/platforms` / `src/trippostcollect/application` / `src/trippostcollect/runtime` |

表内 `records/`、`artifacts/`、`scheduler/`、`xhs/`、`db/bootstrap.py`、`platforms/registry.py` 的旧包路径均补 `src/trippostcollect/`；
`M/` 始终先展开为fork根。符号行锚点以定义为准，局部调用点/字典/模块初始化另行标注。
后文流程表的 `XK:行` 指 `XK/core.py`，`SW/SD/SZ/SX:符号或行` 指各自 `__init__.py`；无新的隐含源码目录。

## B｜确定目录与依赖方向

以下只列本次有迁移职责的文件；现有未受影响模块原位保留。每个分号后的说明是该文件唯一职责。
不建立 contracts/base 大目录；共享端口集中一个文件，平台模型仍由本站定义。

```text
src/trippostcollect/
  application/
    contracts.py          # 进程内窄端口与本规格 D 的输入类型，无实现/IO
    inputs.py             # 根入口 CLI/env 解析、校验、切片装配
    worker_inputs.py      # 仅四站私有 worker argv/env，禁止导入 DB/全平台
    collection.py         # 执行器编排、完成门禁及提交顺序
    reporting.py          # 执行器摘要、样本、markdown 与终态包络；写出前净化
    candidates.py         # 原 AdaptiveAccumulator 计数/决策，注入事件能力
    events.py             # legacy adaptive 事件容错出口；不与严格状态出口合并
    failures.py           # 现有跨入口失败分类与stdout解释；不是平台HTTP异常类
    policy.py             # 原crawl_policy频率/冷却/文件锁策略，平台无写入权
    repair.py             # 通用三站历史详情修复编排
    bilibili_repair.py    # B站历史修复与状态机
    bilibili_promotion.py # B站历史修复晋升
    bilibili_supervisor.py# B站连续修复监督
    warmup.py             # 两个通用 warmup CLI 的编排
    diagnostics.py        # 通用 benchmark 编排
    page_evidence.py      # 原固定页面证据流程
    scrapling_probe.py    # 既有辅助静态探测，不进入五站正式 worker
  platforms/
    entry.py              # 唯一 worker -m 入口，延迟选择 wb/dy/zhihu/xhs
    registry.py           # 现有辅助 WebSite 定义；不是正式 workflow registry
    weibo/{core,client,parser,login,models}.py
    douyin/{core,client,parser,login,models,signer,login_support}.py
    zhihu/{core,client,parser,login,models,signer}.py
    bilibili/{core,client,parser,login,models,signer}.py
    xhs/{core,client,parser,login,models,signer,session,navigation,
         behavior,author,detail,media,repair,manual_wait,errors}.py
  runtime/
    browser.py            # 原 CDPBrowserManager，不承接业务循环
    browser_launcher.py   # 原 BrowserLauncher 的 Chrome 启动/进程所有权
    browser_runtime.py    # 已有子进程 HOME/缓存/崩溃目录边界
    page_readiness.py     # 页面证据专用导航/就绪等待，不替换正式站点导航
    worker.py             # 原 app_runner/main 清理与信号回收
    process.py            # 原 C.run_command、watchdog、管道净化与回收
    behavior.py           # 通用行为执行；XHS 特有回调由入口注入
    human_flow.py         # 原行为 profile、等待、滚动与位移证据
    http.py               # 原 HTTPX 窄工厂，显式 verify，不造 transport 框架
    cookies.py            # Cookie 转换和通用登录 snapshot/export
    login_helpers.py      # QR 字节/Canvas/展示辅助，明确含 IO
    helpers.py            # 共用纯时间/UA/URL转换；随机/时钟显式输入
    image_retry.py        # 原下载重试分类及等待；不拥有 HTTP/提交
  records/
    formal.py             # C 的字段验证、时间/行投影；不发请求
    sanitization.py       # 已有头像键/重复 URL 清理，唯一清理算法
    identity.py           # 原 user_hash 位置；T14 后只做平台原始用户 ID/昵称类型归一，不哈希、不脱敏
  artifacts/
    formal_images.py      # 原C正式图片复验、晋升、回滚与媒体锁；由collection编排
    jsonl.py              # 净化后 JSONL 追加、锁/日期路径/换行
    evidence.py           # 净化后行为、导航、repair 诊断文件出口
    image_staging.py      # 原 fork 整帖 staging/失败行写出；四站 *_store_media 同构实现合并于此
    image_manifest.py     # 已有 manifest schema/读取校验，唯一 schema
    image_materialization.py # 已有独立字节复验、晋升/回滚技术动作
  db/
    discovery_read.py     # 只读已知集合能力；不返回连接，不提交
    content.py            # 执行器要求的内容事务/确定性探测
    bootstrap.py          # 已有 schema 初始化；不由平台 import
  scheduler/runner.py     # R 的调度/计划/收尾，discovery.py 原位
  core/{paths,resources,execution_state}.py # 路径、包资源、严格冻结状态分别唯一
  xhs/{runner,repair}.py  # X/Q 的根编排；既有 config/accounts/leases/runtime/
                         # supervision/terminal/discovery/batch_checkpoint 原位
  resources/js/{stealth.min,douyin,zhihu}.js # 单一源码资源，字节不改
  resources/licenses/MediaCrawler-LICENSE # 保留原许可与来源记录
```

普通站点 `core` 只保留本站 workflow；`client` 拥有请求及 IO 补取；`parser` 只做纯解析/投影；
`login` 拥有本站登录判定；`models` 只包含存活枚举、异常和原 Pydantic 模型；`signer` 保留算法/桥接。
XHS `core` 是 search 与 session 调用编排，不再承载 navigation/author/detail/media 的实现。
`douyin/login_support.py` 专属原 slider/easing 与 phone MEMORY cache，不能被其他站当工具库导入。
包 `__init__.py` 保持无注册副作用；不复制上游 `tools.__init__`、全平台 factory 或 store registry。
bootstrap 可延迟引用实现，平台仅调用注入的 application 端口；application 可在原 worker 内运行，禁止反向 import scripts。
共用工具不 import 平台；XHS 特有行为通过窄 callback 装配。Page/Context 原对象直接借用，不包装每个方法。
依赖方向唯一例外 X1：`records/formal.py` 仅在 `TYPE_CHECKING` 下引用 `artifacts.image_materialization.MaterializedImage`，
运行期不导入 artifacts。`XhsRuntimeSupervisionError` 定义在 `application/contracts.py`，`db/content.py` 与 `xhs/supervision.py`
均从 contracts 导入，避免 db→xhs 依赖。平台 staging 写出经 `contracts.ImageStager` 端口，不直接 import artifacts。
`scripts` 保留外部同名薄入口；E 是私有桥，不作为永久兼容层（T14 已执行：E 已删除）。

## C｜精确迁移清单

本节全部“旧”是 F；箭头右侧是 T。处置“迁”表示保留实际调用顺序、次数、异常与 wire；“拆”只改变所有权。
前置卡见 G；测试代号见 F。同一源码的纯 helper 与 IO 调用分别归位，不整文件复制两份权威实现。

C0–C7 是按能力组织的主链与边界说明；**逐定义的完整处置以[附录 C8](platform-adapter-symbol-ledger.md)为准**。
C8 覆盖 92 个文件、1234 个顶层函数/类/方法，每项有目标模块、处置（迁/拆/并/薄/退）、任务卡和测试责任，
并列出 73 个“退出符号仍被保留代码引用”的调用点及切断方式。C0–C7 与 C8 不一致时以 C8 为准，并在同批修正 C0–C7。

### C0｜五平台迁移闭包总账（2026-09-28 补审）

本账覆盖正式搜索、现存详情修复、登录与诊断入口的十三类依赖；C1–C7 是符号落点，D 是配置值及读取时点，
不再复制第二套接口。`保留`指迁入相应能力，`核心`指留在项目公共层，`牵连`指旧 import 可达但目标须切断，
`退出`指无正式/获保留辅助用途的功能切片，须过 T12/T14 后删除。没有独立文件时明确内联或不适用。
这是静态处置清单；不宣称已观察全部运行时 import，也不把“文件可导入”当作业务必须保留。

| 类别 | B站 article | 微博 | 抖音 | 知乎 | 小红书 |
|---|---|---|---|---|---|
| 入口 | R→C 的 `run_bilibili_article_search`；不经 E/M.main | R→C→E→M.main→WB/core | R→C→E→M.main→DY/core | R→C→E→M.main→ZH/core | X→C→E→M.main→XK/core；独立账号轮次 |
| client | C 内联搜索/详情/粉丝/图请求；urllib 保留 | WB/client.py，HTTPX/详情浏览器回退 | DY/client.py，HTTPX/浏览器响应监听/短链解析 | ZH/client.py，HTTPX/详情页/图片 | XK/client.py，HTTPX/API→HTML/图片 |
| signer | C 的 WBI 常量、keys、`sign_bilibili_wbi_params`，无本站JS | 无独立 signer 或签名JS | DY/help.py 的 `get_a_bogus`；execjs；webid生成属client helper | ZH/help.py 的 `sign`；execjs/Node | XK/playwright_sign.py→xhshow；xhs_sign.py 仅活跃 trace helper |
| login | C 行为会话、W warmup、B repair 三种判别分别保留 | WB/login.py，QR/cookie；phone 空实现不算能力 | DY/login.py，QR/cookie/phone；slider和MEMORY分支保留 | ZH/login.py；QR/cookie，phone 空实现 | XK/login.py、manual_wait.py 与 core 的 session/恢复/扫码守卫 |
| parser | C normalize/clean/extract/hydrate；共享HTML清理 | WB/help.py、client内详情提取、SW投影、E精确ID提取 | DY/search_safety.py 纯部分、help URL解析、SD投影、E精确ID提取 | ZH/help.py 的Extractor/entity/URL判断、SZ投影 | XK/extractor.py、help.py URL解析、core纯判别、SX投影 |
| model | C 三异常及本站常量；不用 M/model/m_bilibili.py | WB/field.py、exception.py；m_weibo.py 无活跃内容模型 | DY/field.py、exception.py、M/model/m_douyin.py 的VideoUrlInfo | ZH/field.py、exception.py、core异常、M/model/m_zhihu.py、M/constant/zhihu.py | XK/field.py、exception.py、core异常；M/model/m_xiaohongshu.py 的NoteUrlInfo |
| store | C 轮末JSONL；共享manifest/SQLite在核心，无fork Bili store | SW/__init__.py、_store_impl.py 的JSONL、weibo_store_media.py | SD/__init__.py、_store_impl.py 的JSONL、douyin_store_media.py | SZ/__init__.py、_store_impl.py 的JSONL、zhihu_store_media.py | SX/__init__.py、_store_impl.py 的JSONL、xhs_store_media.py |
| helper | C Cookie/浏览器/HTML/helper＋现有artifacts；见C5补项 | WB/help.py＋下表共用闭包 | DY/help.py的get_web_id→client；slider/easing/cache＋共用闭包 | ZH/help.py、M/tools/time_util.py＋共用闭包 | XK/help.py/manual_wait.py/extractor.py＋共用闭包；humps→pyhumps |
| JS资源 | M/libs/stealth.min.js；没有签名JS | M/libs/stealth.min.js；没有签名JS | M/libs/douyin.js、stealth.min.js | M/libs/zhihu.js；stealth仅原非CDP分支 | 无本地签名JS；stealth仍在原非CDP分支，不能笼统称无JS资源 |
| 动态import | 本article链无runpy/execjs；资源注入不是Python import | E repair hook 局部import＋共用行为桥 | E repair hook；cache factory局部import＋共用行为桥 | help首签懒编译JS；共用行为桥 | playwright_sign局部import xhshow；E repair/ACK hook＋共用行为桥 |
| 辅助入口 | 两warmup、B repair/晋升/监督；通用benchmark/页面证据 | 两warmup、repair_post_details、benchmark/页面证据 | 两warmup、repair_post_details、benchmark/页面证据 | 两warmup、repair_post_details、benchmark/页面证据 | Q repair；xhs_accounts是核心管理入口；不进warmup/benchmark |
| 配置读取 | 通用jobs→R→C args/env；不消费bilibili_config业务值 | base_config导入weibo_config；cmd_arg CLI覆盖→core/login | base_config导入dy_config；CLI覆盖→core/login；D2作者/fallback env | base_config导入zhihu_config；CLI覆盖→core/login；Cookie/settle env | xhs pool/target→X/C；base_config导入xhs_config后CLI覆盖；D2登录/导航/恢复/ACK env |
| 环境依赖 | 根Python、Playwright/浏览器、Pillow、urllib/SQLite/fcntl；无固有Node需求 | HTTPX/Tenacity/Playwright、Pillow/aiofiles及共用闭包 | 左列能力＋PyExecJS/Node、cv2/numpy、MEMORY任务生命周期 | HTTPX/Tenacity/Playwright、Parsel/lxml/Pydantic、PyExecJS/Node、Pillow/aiofiles | HTTPX/Tenacity/Playwright、xhshow、pyhumps/Pydantic、Pillow/aiofiles及共用闭包 |

五个平台未来 adapter 都只拥有本站请求、签名、登录判别、响应解析、游标解释和鉴权下载，保留本站执行循环。
表中的 store 必须拆成平台纯投影、下载调用与共享净化/JSONL/staging；不能整体搬成 adapter 的长期数据库权限。
B站维持执行器内同步 article；微博保留长文与修复专属回退；抖音保留 response 监听和三元游标；
知乎保留权威 entity 选择与首次签名缓存；XHS拥有轮内会话操作，root仍掌握租约、单轮监督、发现提交和终态。

以下是五站闭包的公共部分；每站矩阵引用本表，不重复抄公共源码。

| 真实边／触发条件（旧文件均相对 M） | 保留能力与退出条件 |
|---|---|
| E.main→`runpy.run_path(main.py)`；main顶层全平台import→CrawlerFactory | 目标仅选站延迟装配；上游Bili/快手/贴吧可被旧入口导入，但不迁其采集能力。旧入口仍在时不能宣称未选平台隔离 |
| `base/base_crawler.py` 的 AbstractCrawler/AbstractApiClient/AbstractLogin/AbstractStore 等被四站继承 | 活跃接口职责由D4端口/本站类型承接；无业务的ABC不整包复制，解除继承引用后退出；不强迫五站统一循环 |
| `tools/utils.py` 星号导入 `crawler_util.py/slider_util.py/time_util.py`；crawler_util反向导回utils并局部导入proxy类型 | UA/时间/Cookie/QR→runtime helper，logger→净化日志出口；不搬聚合utils。`slider_util.py` 顶层cv2/numpy、`easing.py` 顶层numpy使旧四站也有导入牵连；只有拆开后才是目标抖音条件依赖 |
| `tools/trippostcollect_behavior.py` 按env修改sys.path并局部导入human_flow/mediacrawler_behavior | runtime hints/human behavior/request pause/continuity/API captcha/visible state/security limit/post interaction八个桥入口全部记账；通用动作→runtime，XHS特有动作→本站behavior，注入能力，不只替初始run_page_behavior hook |
| `tools/trippostcollect_adaptive.py`→SQLite/env/event；`var.py`→ContextVar和顶层aiomysql | 候选决策/只读集合→application/db；keyword/type显式传给投影；aiomysql类型与无用DB变量在无引用后退出 |
| `config/__init__.py`→base_config/db_config；base_config星号导入七站配置；`cmd_arg/arg.py`赋值全局config | D1/D2只保留实际父参数与平台读点；不迁上游示例ID/DB凭据/全平台注册；Typer/dotenv是否仍需由存活入口核验，不能仅据依赖表删包 |
| 四站store `__init__.py`→`_store_impl.py`→database.models/db_session、SQLAlchemy及Mongo/Excel实现；`tools/async_file_writer.py`顶层→`tools/words.py` | 先抽投影/JSONL/图片；关闭DB/词云不等于未import。words顶层jieba/matplotlib/wordcloud和Excel/Mongo导入随旧出口断开再退出 |
| `proxy/proxy_ip_pool.py`→`proxy/providers/__init__.py`→jishu_http_proxy/kuaidl_proxy/wandou_http_proxy；`proxy/base_proxy.py`→cache factory/types | 正式proxy=false仅说明不调用代理功能；provider导入牵连单列。`cache/cache_factory.py` 的memory→local_cache、redis→redis_cache为局部导入；保留DY MEMORY及`cache/abs_cache.py`接口职责，Redis/proxy支路无引用后退出 |
| `tools/cdp_browser.py/browser_launcher.py/app_runner.py`＋main.async_cleanup/force_stop | 启动、取消、正常关闭、信号回收分别迁runtime；不把platform.close当全部清理；共享浏览器参数和资源见D |
| `tools/httpx_util.py/image_download_retry.py/image_manifest.py/async_file_writer.py/user_hash.py` | HTTP/TLS、重试→runtime；staging/字节→artifacts；脱敏→records；保留净化前后次序，不让平台取得SQLite提交能力 |
| `image_manifest.py` 的 `weibo/xhs/zhihu/douyin_source_asset_key`:97–131、`normalize_image_url`:77、`upsert_manifest_rows_atomic`:220；四站 `*_store_media.py` 同构staging | 稳定键归各站parser，normalize归RT/helpers；upsert与四站staging合并为artifacts/image_staging单实现，平台经ImageStager端口调用（C8/X2、X3） |
| C.ensure_prerequisites、C.profile_dir_for、W.main_async、`scripts/login_warmup.py` 的fork路径；C及四站JS路径 | 即使B站不import上游Python仍依赖fork pyproject/profile/stealth路径；T01替资源定位，T12安装核验，T14旧轮结束后才能删目录 |

公共项目核心**不迁入 adapter**：R调度/冻结计划；C正式字段与完成门禁、媒体事务及通用checkpoint；
`records/sanitization.py/topic_relevance.py`；`artifacts/image_candidates.py/image_manifest.py/image_materialization.py/image_persistence.py/image_completion.py/image_proxy.py/paths.py`；
`db/bootstrap.py/connection.py`与SQL schema；`scheduler/discovery.py`；`core/paths.py`；
XHS accounts/config/leases/runtime/supervision/terminal/discovery/batch_checkpoint。
其中模块可按C1调整到项目包，权限与数据所有者不变；“不迁入adapter”不等于这些文件永远不能为解耦改导入。
页面证据/导入 `scripts/ctf_resource_crawl.py/import_ctf_captures.py` 属独立辅助流程，不能塞进某站正式adapter。

资源内容基线（M 固定SHA；仅计算文件散列，未运行JS）：

| 源资源 | SHA-256 | 目标／校验责任 |
|---|---|---|
| `libs/douyin.js` | `ff5cb3133e2717523ffb3f96679e3f5d23bd31a999411ea604cff9a51da9e26e` | resources/js；T01/T12字节及execjs调用等价 |
| `libs/zhihu.js` | `9753572dc21148975600ca8083e92245e69130cd11414a3e3089013b3aaee3f1` | resources/js；保留Node crypto与首次编译时点 |
| `libs/stealth.min.js` | `02ae012addcdb30b0ed1a512406feb487699b1b217566e87a8086b54dfcc1d4d` | resources/js；逐调用点保留原注入条件；保留其MIT来源头 |
| `LICENSE` | `aeff21de8609bec9d6e939bbbba7c2914ae0a6e7c9470ea7945c03f7d17a2a33` | resources/licenses/MediaCrawler-LICENSE；fork许可不覆盖资源自身来源标识 |

Python/Node/浏览器与安装依赖只记录需求；本轮未运行版本探针、安装或加载。根 `pyproject.toml` 与
M/`pyproject.toml`、锁文件分别是旧环境输入，不把上表能力列表当成已经验证的新根依赖集。

### C1｜共用与根控制

| 旧文件：符号：行 | 新位置及处置 | 前置；测试职责 |
|---|---|---|
| R:`build_command`:321、`resolve_discovery_args`:229、`execute_prepared_job`:829 | `scheduler/runner.py` 迁；child 命令重新冻结 | T01；F01/F10 |
| `scripts/failure_classifier.py:classify_attempt`:316、`extract_stdout_json`:211、`is_xhs_sms_terminal_text`:176 | A/failures；跨入口分类保留，bootstrap向XHS行为注入 `Callable[[str], bool]` 判别能力，不反向import A实现 | T04/T10；原 `tests/test_failure_classifier.py` 与F06/F07/F08 |
| `scripts/crawl_policy.py:site_request_guard`:262、`record_site_cooldown`:395、状态锁/读写:56–121 | A/policy；原文件锁、等待、状态读写一并保留；平台不得自行清冷却或取得策略文件写权 | T04/T10；原 `tests/test_crawl_policy.py` 与F07 |
| `scripts/browser_runtime.py:browser_launch_environment`:15、`browser_runtime_args`:25、`xhs_window_size_value`:34 | RT/browser_runtime；C/W/页面证据所有调用方同批切换，子HOME和窗口读时点不变 | T02/T10；F06/F07/F10 |
| C:`_run_platform_without_policy`:5601、`run_platform`:5928、`apply_formal_completion_gates`:6289 | A/`collection.py` 拆；Bili 原进程提前分支 | T02；F01/F03 |
| E:`main`:724；M/`main.py:CrawlerFactory`:49、`main`:99；M/`cmd_arg/arg.py:parse_cmd` | P/`entry.py`＋A/`worker_inputs.py` 替代 runpy/全局赋值，只装配所选站 | T02；F10 |
| U:`existing_platform_identities`:28、`AdaptiveAccumulator.from_environment`:177（集合加载调用:180） | `db/discovery_read.py`＋A/`candidates.py` 拆；一次加载，不每候选查库 | T03；F01/F09 |
| U:`begin_batch`:190、`is_known`:195、`consider`:205、`skip_candidate_failure`:216、`finish_batch`:286、`mark_source_exhausted`:356 | A/`candidates.py` 迁；停滞计数保留但不作停止条件 | T03/T04；F01/F09 |
| U:`from_environment`:177、`_record_source`:259、`mark_runtime_failed`:390、`summary`:417、`env_int`:15 | candidates迁；微博构造`stagnation_basis="candidate_identity"`、其余`valid_new`显式传入；env_int解析失败回默认值的语义移入worker_inputs零参reader | T03；F01/F09 |
| U:`should_reseed_douyin_frontier`:450，DY/core调用:611 | A/candidates迁，`CandidateDecisions.should_reseed_frontier`只读方法暴露给抖音；四条件（已耗尽、refresh has_more为True/1、next_cursor非空、新候选>0）逐字不变 | T06；F01/F12 |
| C:`is_retryable_image_error`:312、`is_runtime_blocking_image_error`:316；M/`tools/image_download_retry.py`同名:77/83 | RT/image_retry**并**为单实现；合并前比对两组错误码集合（现均为`image_download_retryable`与`image_auth_required/image_rate_limited`） | T04；F05 |
| C:`profile_dir_for`:816、`cookie_snapshot_path`:821、`required_cookie_names`:835；W同名:103/118/112 | core.paths＋RT/cookies**并**；C用`PLATFORMS[..]["mediacrawler"]`、W用`["code"]`，五站代号须先逐一核对一致 | T01/T10；F07/F10 |
| C:`run_command`族:1065–1821（`XhsParentNetworkPauseClock`:224、watchdog:1744/1756、进程组:1694/1704、XHS网络诊断:1343–1380） | RT/process迁；watchdog停止事件仍写严格FrozenExecutionState | T02/T09；F06 |
| C:摘要族:2243–2459、`collect_behavior_validation`:6036、`write_markdown`:6164 | A/`reporting.py`迁；样本与摘要在首次写出前净化 | T11；F04/F10 |
| C:图片正式化族:3597–3896（媒体锁:3722、回滚:3656、manifest复验:3750） | `artifacts/formal_images.py`迁；collection编排调用顺序不变 | T11；F05 |
| C:`ensure_web_schema`:2476、`find_existing_post`:4250、`upsert_web_post`:4268、`commit_formal_import`:4331、`FormalImportBeforeCommitError`:4324 | db/content迁；`XhsRuntimeSupervisionError`回滚分支经contracts类型 | T11；F05/F06 |
| C:`runtime_blocker_*`:6420–6556、`repair_*`:6348/6369/6547 | 前者A/failures，后者A/repair；`repair_runtime_stop_reason`仅测试引用，T00定去留 | T10/T11；F06/F08 |
| `scripts/repair_bilibili_articles.py:23` 从C导入6符号；`scripts/mediacrawler_login_warmup.py:17` 导入`discover_cdp_browser_path` | 两处反向导入改从包内新位置导入，是删除C旧函数的前置条件 | T08/T10；F08/F07 |
| U:`append_execution_event`:131；E:`install_batch_checkpoint_hook`:143 | A/`events.py`＋显式 append→publish；保留吞错边界与 XHS 后续强校验 | T04；F02/F09 |
| ES:`FrozenExecutionState`:72、`append_event`:210 | `core/execution_state.py` 迁严格状态出口；不与 U 的宽容出口混用 | T04；F02/F06 |
| C:`validate_formal_record`:2773、`published_at_for_record`:1173、`row_for_record`:2660 | `records/formal.py` 拆纯验证/投影；完成裁决仍在 collection | T04；F03 |
| E:`sanitize_export_item`:103、`install_export_hook`:117；`records/sanitization.py:sanitize_author_avatar_data`:141 | 复用现有 sanitizer；显式 records→writer 取代 monkeypatch | T04；F04 |
| M/`tools/async_file_writer.py:AsyncFileWriter`:30、`write_to_jsonl`:56 | `artifacts/jsonl.py` 只迁存活 JSONL；实例锁、当日路径、逐条追加不变 | T04；F04/F09 |
| M/`tools/image_manifest.py:ImageAsset`:48、`inspect_image_bytes`:139、`stage_post_images`:294、`failed_manifest_row`:260 | schema 合入现有 manifest；写出进 image_staging，字节复验复用 materialization | T04；F05 |
| M/`tools/image_download_retry.py:fetch_image_bytes_with_retry`:89 | RT/`image_retry.py` 迁，仍接受本站请求闭包 | T04；F05/F09 |
| C:`materialize_formal_record_images`:3896、`import_valid_records`:4335、`import_valid_records_with_media_rollback`:4413 | collection 编排→artifacts 技术动作→db/content 事务；不把平台赋予提交权 | T11；F05 |
| C:`persist_discovery_checkpoint`:2951 | collection→现有 scheduler/discovery；通用发现提交不改成 XHS ACK | T11；F01 |
| M/`tools/trippostcollect_behavior.py:project_browser_args`:18、`install_project_runtime_hints`:29、`run_required_human_behavior`:43 | 输入在入口；RT/behavior、human_flow 接能力，删除 sys.path/scripts 注入 | T02/T04；F07/F10 |
| H:`write_evidence`:76、`visible_page_state`:105、`run_page_behavior`:1216；HF:`BehaviorProfile`:19、`load_behavior_profile`:96、`install_runtime_hints`:123 | RT/behavior、human_flow＋artifacts/evidence 拆；XHS 专用等待/互动进 P/xhs/behavior | T04；F07 |
| M/`tools/cdp_browser.py:CDPBrowserManager`:54、`launch_and_connect`:299、`cleanup`:748 | RT/browser 迁；保留启动 latch、observer、profile及进程身份 | T02；F06/F07 |
| M/`tools/browser_launcher.py:BrowserLauncher`:34、`launch_browser`:226、`cleanup`:433 | RT/browser_launcher 迁；不新增二次 launch fallback | T02；F06/F07 |
| M/`main.py:async_cleanup`:122、`_force_stop`:154；M/`tools/app_runner.py:run`:32 | RT/worker 迁实际清理分派及信号/task 回收；不能只搬 core.close | T02；F06 |
| C:`run_command`:1821、落盘前清理调用:2164、`XhsSupervisorRuntimeReporter`:1453 | RT/process＋原 xhs/supervision 拆；认证监督不等于发现 callback | T02/T09；F04/F06 |
| M/`tools/httpx_util.py:make_async_client`:6；`crawler_util.py:convert_cookies`:138、`convert_browser_context_cookies`:148、`convert_str_cookie_to_dict`:159 | RT/http、cookies 拆；每次 client 关闭/刷新时点不变 | T03；F09 |
| M/`tools/crawler_util.py:find_login_qrcode`:43、`find_qrcode_img_from_canvas`:66、`show_qrcode`:88 | RT/login_helpers 迁；HTTP/截图/Pillow 系统展示均是 IO | T02；F07 |
| 同文件 `get_user_agent`:105、`get_mobile_user_agent`:131、`extract_text_from_html`:215；M/`tools/time_util.py:get_current_timestamp`:30；`user_hash.py:anonymize_user_id`:11、`mask_nickname`:22 | RT/helpers 与 records/identity 拆；随机UA/时钟显式，纯转换不带 slider/cache imports | T03；F03/F09 |
| `src/trippostcollect/core/paths.py:PROJECT_ROOT`:9；C:`ensure_prerequisites`:777、`profile_dir_for`:816；W:`main_async`:512（目录检查:517） | paths 入口兼容；core/resources 替代 fork 文件存在检查 | T01/T12；F10 |

### C2｜微博

| 旧文件：符号：行 | 新位置及处置 | 前置；测试职责 |
|---|---|---|
| WB/`core.py:WeiboCrawler.start`:115、`search`:207、`get_specified_notes`:430、`get_note_info_task`:455 | P/weibo/core 迁 search/detail 两流程、known 提前判断 | T05；F01/F11 |
| 同文件 `create_weibo_client`:656、`launch_browser`:678、`launch_browser_with_cdp`:707、`close`:808 | core 装配→client/RT；保留现有 CDP 失败标准浏览器回退 | T05；F07 |
| WB/`client.py:WeiboClient`:82、`request`:105、`get`:144、`post`:154、`pong`:158、`update_cookies`:174 | client 迁；搜索 Tenacity 5次/3秒，详情直连不同；行为后更新 Cookie | T05；F09/F11 |
| WB/`client.py:get_note_by_keyword`:192；WB/`help.py:filter_search_result_card`:29 | client 请求，parser 过滤 card/card_group；现行过滤后空触发耗尽 | T05；F11/F09 |
| WB/`core.py:get_note_full_text`:738；WB/`client.py:get_note_info_by_id`:299 | core 长文分流；client 详情 HTTP；parser 提取 `$render_data`；不能存截断文本 | T05；F11 |
| E:`install_weibo_browser_detail_fallback`:576、`_find_weibo_detail`:90 | client 显式 repair fallback＋parser 精确 ID；只在 POST_REPAIR，同 Page 锁 | T05；F08 |
| SW/`__init__.py:_first_present`:35、`persisted_weibo_content_text`:134、`update_weibo_note`:140 | parser 投影，core 调 writer；真实0、时间/来源保留 | T05；F03/F04 |
| 同文件 `_weibo_pic_url`:42、`_weibo_pic_assets`:73；WB/`client.py:weibo_image_request_urls`:54、`get_note_image`:332 | parser 图序/pid身份；client URL候选及 Header/Referer/字节请求 | T05；F05/F11 |
| WB/`core.py:get_note_images`:526；SW/`weibo_store_media.py:WeiboStoreImage.store_post_images`:49、`record_failure`:75 | core→RT/image_retry→artifacts/image_staging；请求间隔及 attempts保留 | T05；F05/F11 |
| SW/`_store_impl.py:WeiboJsonlStoreImplement`:213；WB/`login.py:WeiboLogin.begin`:55、`check_login_state`:70、`login_by_qrcode`:85、`login_by_cookies`:124 | JSONL显式出口；login保留 QR/cookie，phone 空实现不宣称可用 | T05；F04/F07 |
| WB/`field.py:SearchType`:28；WB/`exception.py:DataFetchError`:29、`PlatformRuntimeError`:33 | models 迁枚举/异常；空 m_weibo 不造模型，无微博签名JS | T05；F09 |
| M/`tools/time_util.py:rfc2822_to_china_datetime`:101、`rfc2822_to_timestamp`:113，SW投影调用 | RT/helpers保留转换，parser调用；不把抓取时间补为发布时间 | T05；F03/F11 |

### C3｜抖音

| 旧文件：符号：行 | 新位置及处置 | 前置；测试职责 |
|---|---|---|
| DY/`core.py:DouYinCrawler.start`:102、`search`:180、`get_specified_awemes`:734 | P/douyin/core 迁；保留 search 先图片后 store、detail 先 store 后图片 | T06；F12/F05 |
| DY/`client.py:DouYinClient`:59、response 捕获:98、缓存消费:126、滚动:176、首屏回建:224 | client 迁监听task/缓存；最小正offset和部分详情成功按现状 | T06；F12 |
| DY/`search_safety.py:decode_douyin_json_body`:32、`validate_douyin_search_response`:107、`classify_empty_first_page`:142、`inspect_empty_first_page`:150 | 前三者 parser；DOM检查 client，含糊空不降为空页 | T06；F12 |
| DY/`core.py:enrich_aweme_creator`:652；DY/`client.py:get_user_info`:639 | core 缓存与重试，client IO；保留成功/失败缓存、sec_uid 与每次等待 | T06；F03/F12 |
| DY/`client.py:__process_req_params`:306、`request`:392、`get`:413、`post`:421、`search_info_by_keyword`:445 | client；Page navigator/localStorage/Cookie按请求读取；GET params/POST form不改 | T06；F09/F12 |
| DY/`help.py` 模块编译:37、`get_a_bogus`:61；DY/`client.py` 签名选择:388 | signer＋resources/js/douyin.js；搜索URI不签a_bogus，不能统一JSON签名 | T06；F09/F10 |
| DY/`help.py:get_web_id`:39，client调用:370；`client.py:resolve_short_url`:692，core调用:745 | 随机webid helper→client并注入随机源，不是新签名算法；短链IO→client，二次URL解析→parser，保留请求次数与失败 | T06；F08/F09/F12 |
| DY/`help.py:parse_video_info_from_url`:101；M/`model/m_douyin.py:VideoUrlInfo`:26；DY/`client.py:get_video_by_id`:538 | parser/models/client；这些 video 名字仍用于图文，不按名称删 | T06；F08/F12 |
| E:`install_douyin_browser_detail_fallback`:446、`_find_douyin_detail`:81 | client/parser；repair开关才启用，同Page先note再video URL，不下载视频 | T06；F08 |
| SD/`__init__.py:_author_metric`:67、`_normalized_author_stats`:94、`update_douyin_aweme`:287、图资产:155/168 | parser；缺值与0差异、URI去重、最后非空URL保留；来源冲突见 H | T06；F03/F04/F12 |
| DY/`core.py:get_aweme_media`:958、`get_aweme_images`:970；DY/`client.py:get_aweme_media`:674；SD/`douyin_store_media.py:DouYinImage`:38 | core/client→staging；HTTP图片当前未显式附Cookie，不悄悄补鉴权 | T06；F05/F12 |
| SD/`_store_impl.py:DouyinJsonlStoreImplement`:191；DY/`login.py:login_by_mobile`:139 | writer出口；login＋login_support保留phone内存cache、slider/cv2/numpy/easing | T06；F04/F07 |
| DY/`core.py:launch_browser`:886、`launch_browser_with_cdp`:914、`close`:948；DY/`field.py`、`exception.py` | core→RT，现有fallback保留；枚举/异常进models | T06；F07/F09 |
| DY/`field.py:SearchChannelType`:24、`SearchSortType`:32、`PublishTimeType`:38；`exception.py:DataFetchError`:24、`SearchResponseError`:28 | models；保留client默认参数枚举值与异常分流，不只复制VideoUrlInfo | T06；F09/F12 |

### C4｜知乎

| 旧文件：符号：行 | 新位置及处置 | 前置；测试职责 |
|---|---|---|
| ZH/`core.py:ZhihuCrawler.start`:233、`search`:352、`get_specified_notes`:775 | P/zhihu/core；正式CDP，导航后行为前更新Cookie，分页20条offset不改 | T07；F13/F09 |
| 同文件 `_activate_latest_zhihu_page`:124、就绪等待:155、`launch_browser_with_cdp`:982、`close`:1015 | core→RT；现有fallback保留，CDP分支未注入stealth不补注入 | T07；F07 |
| C:`export_profile_cookies`:899；ZH/`core.py:create_zhihu_client`:919 | RT/cookies＋login；snapshot优先，独立导出fallback；要求d_c0/z_c0 | T07；F07/F09 |
| ZH/`client.py:ZhiHuClient`:52、`_pre_headers`:73、`request`:91、`get`:140、`pong`:157、`update_cookies`:177 | client；URI编码后签名同URI；3次/1秒/reraise，404空dict保留 | T07；F09/F13 |
| ZH/`help.py:sign`:241、首次编译:254 | signer＋resources/js/zhihu.js；首次签名编译后复用，Node内建crypto | T07；F09/F10 |
| 同文件 `ZhihuExtractor.extract_contents_from_search`:277、answer:318、article:347、作者:407、`_apply_author_info`:264 | parser；原Pydantic投影，头像证据内存保留到净化 | T07；F03/F04/F13 |
| 同文件 `_find_target_content_entity`:87、`_detail_json_payloads`:131、图片:147、正文:174、merge:219 | parser；扫描script JSON精确ID/type，保留Parsel/lxml树操作 | T07；F13 |
| ZH/`core.py:enrich_search_content_detail`:173、详情观察:655；ZH/`client.py` 详情请求:617/635 | core/client；已有正文图片纯快分支，否则IO补取，request_failed/parse_failed分开 | T07；F13 |
| M/`model/m_zhihu.py:ZhihuContent`:25、`ZhihuCreator`:84；M/`constant/zhihu.py`:24 | models；只迁活跃模型/URL常量，不因import自动搬Comment | T07；F03/F13 |
| ZH/`field.py:SearchTime`:27、`SearchType`:40、`SearchSort`:50；`exception.py:DataFetchError`:24、`PlatformRuntimeError`:28；core的`ZhihuImageDownloadError`:65、`ZhihuDetailFetchError`:79 | models，覆盖搜索默认参数及详情/图片错误；作者enrichment仍需ZhihuCreator，不能随creator批采一起删 | T07；F09/F13 |
| ZH/`help.py:judge_zhihu_url`:704 | parser；detail/repair入口URL分流保留，不另写只适用于search的解析器 | T07/T10；F08/F13 |
| SZ/`__init__.py:zhihu_content_image_assets`:41、`update_zhihu_content`:108；ZH/`client.py:get_content_image`:202 | parser/core/client；公式/头像图排除，图片保留Cookie/UA/proxy/Referer，不签内容API | T07；F05/F13 |
| SZ/`zhihu_store_media.py:ZhihuStoreImage`:20、`_store_impl.py:ZhihuJsonlStoreImplement`:180；ZH/`login.py:ZhiHuLogin`:36 | staging/writer/login；QR/cookie保留，phone为空；worker pong与warmup谓词独立 | T07；F04/F07 |

### C5｜B站 article

| 旧文件：符号：行 | 新位置及处置 | 前置；测试职责 |
|---|---|---|
| C:`run_bilibili_article_search`:5020、已有集合加载调用:5066 | P/bilibili/core，同执行器同步流程；一次读集合在行为浏览器之前 | T08；F01/F14 |
| C:`run_bilibili_behavior_session`:1002 | core/login→RT行为；导出Cookie后finally关闭Context，再urllib | T08；F07/F14 |
| C:`normalize_bilibili_article_record`:4470、`clean_bilibili_article_body`:4512、`normalize_bilibili_detail_image_url`:4524、`extract_bilibili_detail_images`:4531 | parser；Opus＋HTML＋content_pic_list合并，仅无图再fallback | T08；F14 |
| C:`fetch_bilibili_wbi_keys`:4917、`sign_bilibili_wbi_params`:4936、`fetch_bilibili_article_page`:4949 | client/signer；轮初key，注入wts时钟；搜索urllib/排序编码不改 | T08；F09/F14 |
| C:`fetch_bilibili_article_detail`:4780、`fetch_bilibili_article_detail_with_retry`:4833、`hydrate_bilibili_article_record`:4861 | client重试、parser hydrate；hydrate不覆盖搜索发布时间/作者 | T08；F03/F14 |
| C:`fetch_bilibili_follower_count`:4979；搜索处理调用:5153/5253/5306/5382 | client/core；known→详情→图片→粉丝→净化→裁决，不交换顺序 | T08；F03/F14 |
| C:`bilibili_image_headers`:4604、`fetch_bilibili_image_bytes`:4616、`download_bilibili_record_images`:4630 | client/core→staging；Cookie/article Referer，适用3次重试 | T08；F05/F14 |
| C:批次事件调用:5435、失败前沿:5466、轮末JSONL:5509 | core注入events/writer；保留轮末sort_keys写出，不能改为四站逐条时点 | T08；F01/F09 |
| C:`BilibiliArticleDetailError`:269、`BilibiliFollowerFetchError`:289、`BilibiliRuntimeBlocked`:305 | models；retryable/code/attempts/waits元数据保持 | T08；F09/F14 |
| C:WBI常量:181–222（`BILIBILI_WBI_MIXIN_TABLE`:216）、`bilibili_detail_headers`:4591、`clean_html_text`:2259；Cookie/browser helpers:782–869 | WBI常量→signer，headers→client，HTML纯清理→parser共享helper；Cookie/浏览器定位→RT，不能复制成本站第二套资源管理 | T08；F07/F09/F14 |
| C:`fetch_bilibili_image_bytes`:4616→`artifacts/image_proxy.py:fetch_remote_image_bytes`:155 | client调用现有共享URL/重定向/字节门禁，image_proxy原位保留；不在adapter重写下载安全检查 | T08；F05/F14 |
| B:`fetch_repair_article_detail`:621、`check_bilibili_login`:628、`prepare_search_record`:503、`apply_repaired_record`:1012 | 请求复用client；应用修复/SQL留A/bilibili_repair与db；URL-only历史修复不等于正式图片完成 | T08/T10；F08/F14 |

B站登录不是一个统一谓词：C行为会话导出当前Cookie；W/`current_state`:274的B站分支接受
SESSDATA或DedeUserID；B修复要求SESSDATA且nav.isLogin。迁移不顺手统一。C:1008/1029在stealth文件存在时才注入，
目标包应包含资源，但不能把原缺失时跳过的运行行为悄改为失败；资源完整性由安装门禁另验。

### C6｜XHS 平台与根边界

| 旧文件：符号：行 | 新位置及处置 | 前置；测试职责 |
|---|---|---|
| X:`_run_main`:860、`main`:1897、`build_child_command`:456、300011控制:1647；Q:`_run_main`:376、`build_child_command`:247 | 根 xhs/runner、repair；不迁平台、不进通用warmup | T09/T10；F06/F08 |
| X:`_terminal_failure_fields`:556、`_challenge_reason`:626、`_login_reason`:672、`upsert_run`:818 | 根runner共享报告/DB调用；Q改包内导入 | T09；F06 |
| XK/`core.py:XiaoHongShuCrawler.start`:1662、`_run_browser_session`:1702、`search`:1809 | P/xhs/core编排→session；保留独立分页及refresh/frontier，不统一五站循环 | T09；F01/F15 |
| 同文件 `_validate_login_contract`:261、`_profile_dir`:934、`create_xhs_client`:2842 | worker_inputs校验；session借root路径/Context，登录后/行为后/恢复后刷新Cookie | T09；F07/F09 |
| 同文件 `_install_new_page_guard`:292至`_prepare_browser_shutdown`:406、`_close_page_with_deadline`:805、存活检查:940/979 | session；守卫task、置前/保留/关闭，主Page死即失败，不接管辅助页 | T09；F07/F15 |
| 同文件 `_goto_with_deadline`:463、observer:583、shell:707、search导航:750、诊断:619–705 | navigation＋artifacts/evidence；同Page恢复，无轮内重启权 | T09；F07/F15 |
| 同文件 `_run_qrcode_login`:986、`_single_page_for_login`:1053、初始观察:1077–1116；XK/`login.py:XiaoHongShuLogin`:36 | session/login；180秒刷新下限、扫码锁存、600秒共享预算及稳定确认整体迁 | T09；F07/F15 |
| XK/`manual_wait.py` 三类定义:19/29/75；core惰性budget:285 | manual_wait原样迁，全轮一个实例，嵌套ticket/暂停不重置 | T09；F07/F15 |
| XK/`core.py` 错误解释:1119–1194、popup:1196–1286、网络恢复:1288–1532、`_wait_for_midrun_login_recovery`:1534 | errors/session；分类优先级和原预算保持 | T09；F09/F15 |
| 同文件 `enrich_note_creator`:2475、作者恢复:2528–2676、`get_note_detail_async_task`:2710、`get_note_images`:2987 | author/detail/media；IO不进parser；API→HTML、semaphore及成功作者缓存保持 | T09；F05/F15 |
| 同文件 `is_video_note`:2679、`note_detail_summaries`:2684、互动:879；H的XHS等待/互动:194–1213 | parser纯判别、净化诊断；P/xhs/behavior特有动作，通用动作RT/human_flow | T09；F04/F07 |
| 同文件 `launch_browser`:2873、`launch_browser_with_cdp`:2900、`close`:2930 | session→RT；一次launch/单Context、不fallback；正常嵌套关闭分派保留 | T09；F06/F07 |
| XK/`client.py:__init__`:90、`_pre_headers`:131、`request`:175、`get`:280、`post`:302、`get_note_media`:322 | client；原HTTPX短会话、JSON/body编码、重试和图片无显式Cookie/Header保持 | T09；F09/F15 |
| 同文件 `query_self`:346、`pong`:392、`update_cookies`:414、search:430、detail:462、creator:689、HTML:831 | client；保持原请求、探测及刷新，不把HTTP客户端数当Context数 | T09；F09/F15 |
| XK/`playwright_sign.py:sign_with_xhshow`:33；`xhs_sign.py:get_trace_id`:150 | signer；延迟xhshow import，每次Xhshow()；保留trace随机fallback，不搬无调用旧算法 | T09；F09 |
| XK/`help.py:base36encode`:252、`get_search_id`:278、`parse_note_info_from_note_url`:304；`extractor.py:XiaoHongShuExtractor`:27 | parser纯URL/INITIAL_STATE；search ID生成进core并注入时钟随机；不将IO带入parser | T09；F03/F15 |
| M/`model/m_xiaohongshu.py:NoteUrlInfo`:27；XK/`field.py:SearchSortType`:55、`SearchNoteType`:65；`exception.py`:24–41 | models/errors；NoteUrlInfo三字段与枚举值保持，带token URL不进普通配置摘要 | T09；F08/F09 |
| XK/core的`XHSImageDownloadError`:176、`XHSNoteDetailUnavailable`:190、`XHSCreatorProfileUnavailable`:202、`XHSNetworkRecoveryTimeout`:214；client的`unwrap_xhs_request_failure`:63、`is_recoverable_xhs_transport_failure`:74 | 四异常→errors；transport展开与可恢复判断→client；session消费分类结果，保留原错误优先级 | T09；F09/F15 |
| SX/`__init__.py:update_xhs_note`:211、纯helper:37–154、图片更新:361、失败:367 | parser投影；core显式keyword/type/时钟，media→staging，保留完整payload清理证据 | T09；F03/F04/F05 |
| SX/`_store_impl.py:XhsJsonlStoreImplement`:104、`xhs_store_media.py:XiaoHongShuImage`:42 | JSONL/staging存活切片迁；DB/Mongo/Excel工厂引用退出 | T09；F04/F05 |
| E:`install_xhs_repair_resilience`:298、blocking:176、failure:210、scope:257、blocker:266、report:243 | P/xhs/repair＋artifacts/evidence；同Context分批gather/逐候选分流，不启发现ACK | T09；F08 |
| XL:`LeaseGuard`:1724；XT:`linearize`:205、`finish`:274；BC:`publish_batch`:86、`BatchCheckpointCommitter`:198；XD:`commit_child_discovery`:192 | **根模块原位保留**；publish代码归根但在worker调用，committer在root事务后ACK | T09/T11；F02/F06 |

### C7｜辅助入口、条件与删除边界

| 旧文件：符号：行 | 新位置及处置 | 前置；测试职责 |
|---|---|---|
| `scripts/login_warmup.py:run_target`:96；W:`warmup_one`:353、`write_cookie_snapshot`:130、`weibo_api_check`:248 | A/warmup；snapshot chmod/重开验证保留；微博warmup要求login+uid，worker pong仅login | T10；F07/F08 |
| `scripts/repair_post_details.py:_detail_target`:128、`build_child_command`:366、`run_repair_child`:419；C:`load_post_repair_targets`:688 | A/repair；ID/URL精确绑定、禁checkpoint，不扩大发现范围 | T10；F08 |
| Q:`_detail_url`:129、`select_targets`:198 | 根xhs/repair→本站parser URL；人工账号及既有详情目标，非新搜索 | T10；F08 |
| B:`run_repair`:1424、`run_continuous_repair`:1737；`scripts/promote_bilibili_repair_results.py:promote_successes`:263；`scripts/run_bilibili_full_repair_supervisor.py:run_supervisor`:235 | A的三份Bili辅助模块，脚本薄包装；内容与修复状态事务不合并 | T10；F08/F14 |
| `scripts/info_collection_benchmark.py:run_mediacrawler_job`:130、`run_ctf_resource_job`:230；`ctf_resource_crawl.py` 主流程；`ctf_scrapling_preflight.py:run_scrapling_static_preflight`:175 | A/diagnostics、page_evidence、scrapling_probe；保留辅助driver，benchmark排除XHS | T10；F10 |
| `scripts/ctf_browser_resilience.py:page_readiness_state`:99、`wait_for_content_ready`:134、`wait_for_content_enrichment`:152、`navigate_with_commit_and_readiness`:183；抖音Cookie清理:32/72 | 就绪/导航→RT/page_readiness；仅页面证据使用的Cookie清理→A/page_evidence；不推广为正式DY登录逻辑。`scripts/import_ctf_captures.py`保留独立导入入口 | T10；F10补页面证据就绪/Cookie场景，当前未发现同名专项测试 |
| `platforms/registry.py:WebSite`:23、SITES:50；HF:`BehaviorProfile`:19 | registry仍辅助资料，按实际调用拆输入；XHS warmed-profile notes是旧文字，知乎preferred_engine=patchright不是正式驱动选择 | T03/T10；F07/F10 |
| M/`tools/slider_util.py:Slide`:34、`get_tracks`:178；M/`tools/easing.py:get_tracks`:77；M/`cache/cache_factory.py:create_cache`:34、M/`cache/local_cache.py:__del__`:48 | P/douyin/login_support；保留内存清理task及临时图片生命周期，其他平台不import | T06；F07 |
| M/`cache/abs_cache.py:AbstractCache`:31；factory局部import memory/redis:43/46；slider的easing局部import:182 | MEMORY接口/实现→DY login_support，退出Redis factory支路；`temp_image`临时文件由paths定位，保留生成/清理时点与QR系统展示要求 | T06/T12；F07/F10 |
| M/`store/*/_store_impl.py` 同文件非JSONL实现、`tools/words.py:AsyncWordCloudGenerator`:36 | 拆活跃投影/下载/JSONL后，删DB/CSV/Excel/Mongo/词云牵连；不能整体先删store | T12；F04/F10 |
| M/`main.py:CrawlerFactory`:49、`config/base_config.py` 星号导入:145、`var.py` DB类型、proxy provider导入 | 退出全注册/全局config/aiomysql/Redis牵连；正式proxy=false不表示旧import没有副作用 | T12；F10 |
| M/`libs/douyin.js:sign_datail`:429、`sign_reply`:433；`libs/zhihu.js:get_sign`:155；`libs/stealth.min.js`:1 | resources/js原字节；版权头/来源SHA保留；选站才装载，详见 G/T12 | T01/T12；F10 |

未迁功能的删除条件是所有五站及上述辅助入口无静态/动态引用且测试职责已迁，不是文件名像“video/store/UI”。
明确退出：fork Bili视频主循环、快手/贴吧注册、评论/creator批采、上游DB/GUI/webui/文档npm构建。
保留：抖音图文复用VideoUrlInfo/get_video_by_id、合法登录slider/MEMORY cache、正文下载/manifest/JSONL。
requests直接调用的已审计支路是贴吧；其支路退出后才裁根显式依赖。lxml仍属知乎Parsel及根Scrapling闭包。
HTTPX、Parsel、Pydantic、Pillow、Tenacity、execjs/Node、xhshow、aiofiles、pyhumps及cv2/numpy沿用锁定能力。
cv2/numpy在旧utils链为导入依赖；拆开后才限于抖音登录支路，见C0，不能提前从旧worker环境删除。

退出切片补账：DY `help.py:parse_creator_info_from_url`/`model/m_douyin.py:CreatorUrlInfo` 与
XK同名helper/`model/m_xiaohongshu.py:CreatorUrlInfo` 的已定位调用在creator批采；ZH `ZhihuComment`在评论链；
DY旧 `get_a_bogus_from_playwright`、四站评论store、`douyin_store_media.py:DouYinVideo`和XHS视频store为删除候选。
这些项不进入目标正式接口；T00核对辅助入口和测试剩余引用，T12迁完保护语义、T14才删除。
这不包含图文复用的VideoUrlInfo/get_video_by_id，也不包含作者enrichment模型；未执行动态引用核验前不宣称代码已经不可达。

## D｜输入、读取时点与窄接口

### D1｜配置和指纹：逐键保留覆盖规则

阶段：I=原模块导入，B=入口解析，S=搜索/会话起点，L=轮内操作，F=收尾。FP是查询指纹，非计划哈希。
入口将原存活读项显式注入；Cookie、可变会话、时钟/随机、scope reader不放入普通配置。
原模块导入取值在新入口等价边界捕获，操作起点才读取的项用绑定到已解析输入的零参reader取值；
reader不得暗读env/config，也不得把TLS每次建client、Cookie显式刷新、人工budget惰性创建提前。
`core.paths` 的 `TRIPPOST_PROJECT_ROOT` 入口兼容原位保留；不要求本期重写整个根项目路径模块。

| 完整键路径／默认与优先级（F） | 生产者→消费者、时点 | T归属；FP/敏感性 |
|---|---|---|
| `schema_version` 通用2；XHS pool2/target3 | R:`validate_crawl_config`:128；xhs/config:`load_pool_config`:31、`load_target`:57；B | 入口校验；不入FP |
| `jobs[].enabled` 缺省true，`priority/schedule_seconds/max_attempts` 100/86400/2，`next_run_at` 缺省iso() | db/bootstrap:`sync_config_jobs`→R:`select_due_jobs`:155；B/F | scheduler；不入FP，enabled/到期是通用调度门 |
| `jobs[].params.platform` 否则site_key；`.keyword` 恢复CLI优先→params值→`青岛旅游` | R:`build_command`:321→C；B | QueryInput；平台/关键词入FP |
| `jobs[].params.login_type` cookie；`.headless` 显式false且未--headless或--headful才headed | 同上；B/S | 登录/浏览器切片；五排除项内，FP=N |
| `jobs[].params.timeout_per_platform` 已删除（#75），出现即配置错误；无进展看门狗为代码常量 `NO_PROGRESS_WATCHDOG_SECONDS`=1200，所有平台统一 | R:`validate_crawl_config`/`build_command`拒绝；C:`_run_platform_without_policy`→`run_command`；B/L | 父监督；不入FP，不替换请求timeout |
| `jobs[].params.required_fields_profile` / `.followers_policy` 正式必需现值 `image_post_with_followers_v1` / `required` | R:342–351校验→C字段门禁；B/L | application；FP=N |
| `jobs[].params.top_refresh_max_pages` 续跑int(value or 3)，首次0 | R:`resolve_discovery_args`:229；B/S | DiscoveryInput；FP=N |
| **`jobs[].behavior_profile`** 字符串或name对象，正式social_high_risk | db/bootstrap→R:`behavior_profile_name`:181；B/L | 行为切片；顶层不入source_query_options，FP=N |
| **`jobs[].params.behavior_profile`** 或其他非排除params键 | source_query_options直接保留，不因没消费而排除 | FP=Y；不得以新分类“运行项”私自去掉 |
| `defaults.schedule_jitter_ratio` 缺失/假值0；`defaults.headless/import_results` 此调度链无消费 | R:`next_run_time`:542；F | 前者调度，后二者不新增语义；均不入FP |
| XHS target `target_key/keyword`；`top_refresh_max_pages` 必需，首次0；`timeout_seconds` 已删除（#75），出现即配置错误 | XD:`resolve_discovery_plan`:40；B | target_key/keyword入FP；顶部刷新不入FP |
| XHS pool `headed=true/behavior_profile=xhs_guarded`；`lease_seconds` 为父层心跳续期的租约 TTL，至少850；CLI account_id必需 | X→租约/child；B | 账号独立scope不入FP，profile路径非Cookie |
| fork `PLATFORM=xhs/LOGIN_TYPE=qrcode/CRAWLER_TYPE=search/START_PAGE=1` | base_config导入本站config→cmd_arg CLI覆盖→core；I/B/S | worker仅保留父生成参数；不把上游示例关键词/ID设成公共默认 |
| `LOGIN_TYPE` 实例构造回写与begin读取 | WB/login:48→58–62、DY/login:46→63–67、ZH/login:45→68–72；S/L。XHS/login:136拒绝非qrcode，不走该回写 | 目标登录实例保存原构造输入，begin按同一实例值分流；不保留全局赋值，保持单worker/单登录实例现状与拒绝时点 |
| `HEADLESS/CDP_HEADLESS/ENABLE_CDP_MODE=false`；`SAVE_LOGIN_STATE=true`，`USER_DATA_DIR=%s_user_data_dir` | CLI headless覆盖前两项→runtime；B/S | 浏览器参数切片；XHS不使用长期profile分支；T14-C 已删除 `USER_DATA_DIR` 配置键，非XHS profile 由 core.paths 定位到 `data/runtime/platform_sessions/<platform>/` |
| `CDP_DEBUG_PORT=9222/BROWSER_LAUNCH_TIMEOUT=60/CDP_CONNECT_EXISTING=false/AUTO_CLOSE_BROWSER=true` | base_config→CDP/launcher；S/F | 浏览器参数切片；XHS拒接已有浏览器 |
| `MAX_CONCURRENCY_NUM=1/CRAWLER_MAX_SLEEP_SEC=2/SAVE_DATA_OPTION=jsonl/SAVE_DATA_PATH=''` | 父显式并发1、jsonl、本轮路径；B/L | workflow/writer各取所需，不全塞context |
| `ENABLE_GET_MEIDAS=false/ENABLE_GET_COMMENTS=true/ENABLE_GET_SUB_COMMENTS=false` | 父覆盖图片布尔、评论false/子评论false；B/L | 保留旧拼写wire；不引入视频模式 |
| `DISABLE_SSL_VERIFY=false` | make_async_client每次构造读取；显式verify优先；L | RT/http工厂；保持HTTPX trust_env，不统一长连接 |
| `WEIBO_SEARCH_TYPE=default/ENABLE_WEIBO_FULL_TEXT=true`；`PUBLISH_TIME_TYPE=0`；`SORT_TYPE=popularity_descending/XHS_INTERNATIONAL=false` | weibo_config:25/42、dy_config:22、xhs_config:25、base_config:27→所选平台；S/L | 各站options；原固定值不自动纳入FP，来自params则服从原投影 |
| fork proxy=false/pool_count=2/provider=kuaidaili/static URL空；词云false、notes15/comments10 | 父禁proxy/评论；关闭分支 | 不迁为正式数量/存储能力；代理URL可能秘密，不输出 |

通用精确算法：`scheduler/discovery.py:source_query_options:25` 仅去掉 `platform/keyword` 和五项
`followers_policy,headless,login_type,required_fields_profile,top_refresh_max_pages`。
`query_fingerprint:33` 对 `{platform_key,keyword,source_options}` 用 `canonical_json:21`
（ensure_ascii=False、sort_keys=True、separators=(',', ':')）编码后SHA-256；禁止对新dataclass全量hash。
XHS `xhs/discovery.py:xhs_query_fingerprint:12` 仍以platform=xhs、keyword、`{target_key}`投影；account另作表scope。
`WebSite`（registry:23）仅将实际用到的delay/jitter/session/daily/cooldown/risk/wait/engine/mobile/profile/referer给对应辅助调用方。
其中 XHS warmed-profile notes不进入正式配置；知乎辅助preferred_engine=patchright不推翻正式CfT/CDP。
HF:19 的 BehaviorProfile保留name、list/detail/cooldown区间、comment概率、max_details、scroll次数/幅度、touch概率、mouse次数；
HF:96仍按命名profile及原支持的override解析，不能用新总预算替代。旧说明文字清理列 H/R05，不改运行代码。
行为默认数值（HF:33–80，顺序为list/detail/cooldown秒、comment概率、max_details、scroll次数/幅度px、touch概率、mouse次数）：
`social_high_risk=(30–150,60–240,240–1200;0.28;1;4–10/320–1150;0.55;2–6)`；
`xhs_guarded=(45–120,90–240,600–1800;0;1;2–5/360–980;0;1–3)`；
辅助`conservative=(18–75,45–180,120–600;0.18;2;3–8/420–1500;0.35;2–6)`、
`quick_probe=(3–12,6–24,5–20;0.03;1;1–3/300–900;0.10;2–6)`。
`load_behavior_profile(name_or_site=None, overrides=None, strict=False)`缺名/非strict未知名回conservative；strict未知名报错，override仅更新已存在且非None键。

### D2｜CLI 与 env 兼容清单

以下CLI名省略 `--`；b=store_true默认false，?=None，p=路径。表内组表示每个同名参数分别存在，不新增组合参数。
路径默认沿用原 `core.paths` 常量；敏感URL/token/Cookie不输出到普通配置摘要。现行失效字段继续拒绝。

| 入口和源码解析锚点 | 保留的参数、类型与默认；生产/消费时点 |
|---|---|
| R:`parse_args`:77 | db/config/run-root/execution-state-root=p对应常量；max-jobs=3/max-parallel-platforms=4；site/kind/job-key=?；start-page/resume-summary/recovery-keyword=?；sync-only/no-sync-config/dry-run/headless/headful/no-throttle/no-import=b；B→调度及命令 |
| C:`parse_args`:425 | keyword=青岛旅游，platforms=weibo,douyin；output-dir/db/media-root=p对应常量；login-type=cookie/required-fields-profile=image_post_with_followers_v1/behavior-profile=social_high_risk；B→执行器 |
| 同上恢复/开关 | get-media/download-images/headed/no-checkpoint-write/no-import=b；start-page=1/start-offset=0/start-cursor=''；resume-summary=?/top-refresh-max-pages=0/discovery-source-exhausted=b；get-media解析后拒绝；B→workflow |
| 同上scope | discovery-job-id:int?、discovery-query-fingerprint/discovery-run-id:str? 三者成组；xhs-account-id/xhs-profile-dir/xhs-discovery-target-key/xhs-discovery-query-fingerprint=?；B→对应独立scope |
| 同上修复 | zhihu-detail-urls-file/xhs-detail-urls-file/xhs-repair-target-ids-file/repair-targets-file=p?；xhs-repair/post-repair=b且互斥；xhs-repair-batch-size=5；xhs-post-interaction=none（none/comment-scroll/like-one/random）；B/L→本站详情，不启发现写 |
| `scripts/login_warmup.py:parse_args`:31 / W:parse_args:70 | targets=all / platforms=douyin,zhihu；timeout-seconds=600/output-dir=各自常量/browser-path=?；前者list-targets=b，后者no-close-on-success/skip-reopen-verify=b；B/S/F，不含XHS |
| X:parse_args:130、Q:parse_args:84 | X:target-key/account-id必需，db/target-config/pool-config=常量，dry-run/no-import/retry-on-300011=b，post-interaction=none；Q:target-key=qingdao_travel、account-id必需、相同路径、keyword=?、max-items=20/batch-size=5/post-id追加[]/dry-run=b/post-interaction=none；B |
| `scripts/xhs_accounts.py:parse_args`:28 | db；ensure-slot/retire需account-id；quarantine/activate另需reason；recover-orphan-lease需account-id/run-id/lease-id；list；逻辑槽位管理原位保留 |
| `scripts/repair_post_details.py:parse_args`:48 | platform必需douyin/weibo/zhihu，db，keyword=青岛旅游，batch-size=20/max-items=0/post-id追加[]；headless/dry-run/confirm-default-db-repair=b；B |
| B:parse_args:2020 | db；state-db/report-dir/create-backup/backup-path/only-ids-file=?；expected-baseline-sha256=''；apply/confirm-default-db-repair=b；max-items/session-size=10；pacing-min/max=原BILIBILI_DETAIL_PACING_SECONDS；session-pause-min/max=8/15；retry-delay-seconds=300/source-limit=0；exclude-retryable-id追加[]/operator-exclusion-reason=?/confirm-operator-exclusion/continuous/stop-when-scope-attempted=b；B/L |
| `scripts/promote_bilibili_repair_results.py:parse_args`:533 / `scripts/run_bilibili_full_repair_supervisor.py:parse_args`:324 | promotion:target-db默认，其余staged-db/state-db/backup-path/report-dir/expected-target-sha256必需，apply/confirm-default-db-promotion=b；supervisor:pilot-db/pilot-state/source-backup/expected-target-sha256/pre-full-backup/full-state/pilot-report-dir/promotion-report-dir/full-report-dir/supervisor-report必需、target-db默认、confirm-default-db-full-repair=b、source-limit/max-items=100/session-size=1/pacing-min/max=90/120/session-pause-min/max=90/120/retry-delay-seconds=300；B/L |
| `info_collection_benchmark.py:parse_args`:33 | keyword=青岛旅游；db/config/output-dir常量；sites=?/headless=b；通用诊断B |
| `scripts/ctf_resource_crawl.py:parse_args`:214 | sites/urls=?/site-label=custom/configured-site-urls=b/output-dir常量/keyword=''；headless/no-throttle=b/max-scrolls=6/behavior-profile=''；timeout/commit-timeout/readiness-timeout=60000/12000/15000；settle-min-ms/max-ms=2000/6000；douyin-cookie-cleanup/scrapling-preflight=auto（auto/off）/scrapling-preflight-timeout=30；B/L辅助路径 |
| C:5622生成→新worker parser | platform/lt/type/keywords/get_comment/get_sub_comment/get_media/headless/save_data_option/save_data_path/start/max_concurrency_num/enable_ip_proxy全部显式传；条件specified_id/enable_cdp_mode；保留str2bool输入集合，不改成根store_true语义 |

新worker唯一模块名为 `trippostcollect.platforms.entry`，`main(argv: Sequence[str] | None = None) -> int`，
只接受父侧生成的私有选项及已有条件模式；未传必需父字段时明确解析失败，不使用上游示例ID抓取。
外层CLI仍兼容；上游私有 `init_db/cookies/creator_id/max_comments_count_singlenotes/crawler_max_notes_count`
及proxy扩展不变成公共承诺。旧worker随旧轮运行；新轮命令冻结使用-m，不能切换半轮。

env表 `E_` 精确展开为 `TRIPPOSTCOLLECT_`；除特别标记均非秘密、FP=N，传递已有fingerprint不再次hash。
父构造extra_env覆盖继承环境；setdefault项保留继承值。没有全局“CLI > env > config”规则。
事实源：C:5622–5805、U:28/131、M/config/base_config:31/71、XK/core的各操作及login:713–749、BC:25–27/86。

| env精确名称/默认或父覆盖 | 生产→消费及原时点；目标能力 |
|---|---|
| `TRIPPOST_PROJECT_ROOT`（注意无COLLECT）源码根回退 | operator→core.paths，I；保留登记的路径入口例外 |
| `E_CUSTOM_BROWSER_PATH`优先于`CUSTOM_BROWSER_PATH`，均空回退 | operator/C→发现/CDP，B/S；浏览器参数切片 |
| `E_COOKIES` 默认空，知乎父导出 | C→client，I/S；**秘密**，内存CookieReader；XHS拒绝旧登录输入 |
| `E_STRIP_AUTHOR_AVATARS=1`父固定 | C→serializer，B/L；非1仍拒绝，首次持久序列化强制清理 |
| `E_DB_PATH/E_RESUME_IDENTITIES_PATH` 无默认 | C→reader，S；绑定路径，不把SQLite连接给平台 |
| `E_EXECUTION_STATE_PATH` 空时legacy事件直接return | R/X→C/事件/BC，S/L/F；分别注入legacy sink/严格reader |
| `E_DISCOVERY_JOB_ID/E_DISCOVERY_QUERY_FINGERPRINT` 空 | C→reader，S；通用scope，job转换错误按旧容错 |
| `E_DISCOVERY_RESUME_OFFSET` 非负int，缺省按页推导；`E_DISCOVERY_RESUME_CURSOR`空 | C→DY/XHS，逐关键词S；env_int解析回退保持 |
| `E_DISCOVERY_TOP_REFRESH_MAX_PAGES=0/E_DISCOVERY_SOURCE_EXHAUSTED` 精确1 | C→四站phase构造，逐关键词S；DiscoveryInput |
| `E_DISCOVERY_RUN_ID/PLATFORM/KEYWORD/RESUME_PAGE/CHECKPOINT_WRITE_DISABLED`（均补E_DISCOVERY_前缀） | 父发但已审计闭包无消费者；T12移除发出，C自身禁写门禁与START_PAGE保留 |
| `E_HUMAN_BEHAVIOR_ENABLED=1/E_HUMAN_BEHAVIOR_PROFILE/E_HUMAN_BEHAVIOR_EVIDENCE` | C→行为桥，L；行为执行与证据出口分开注入 |
| `E_PROJECT_SCRIPTS`；`E_BROWSER_ARGS_JSON=[]` | 前者桥动态导入删除；后者S解析list[str]，保留lang追加 |
| `E_SHARE_CDP_PROFILE=1` XHS/知乎父设；`E_CLEAN_BROWSER_TABS=1`知乎父设 | C→CDP，S；共享参数不等于XHS跨轮profile |
| `E_POST_REPAIR=0/1`；`E_WEIBO_BROWSER_DETAIL_TIMEOUT_MS=30000`父未覆盖 | C/operator→微博修复fallback，B/L；仅原修复条件 |
| `E_DOUYIN_ENRICH_CREATORS=1/ENRICH_ONLY_IMAGES=1/MAX_CREATOR_ENRICH=-1/CREATOR_SLEEP_SECONDS=0.25`（各补E_DOUYIN_） | C父覆盖；max原默认30，only原默认1；作者操作L，非候选数量停止 |
| `E_DOUYIN_BROWSER_DETAIL_FALLBACK` 搜索0/修复1；`E_DOUYIN_BROWSER_DETAIL_TIMEOUT_MS=30000` | C→repair client，B/L；现有Page锁，不开第二浏览器 |
| `E_ZHIHU_INITIAL_SETTLE_SECONDS` core默认0、父8 | C→core，登录settle L |
| `E_XHS_ACCOUNT_ID/E_XHS_DISCOVERY_TARGET_KEY/E_XHS_DISCOVERY_QUERY_FINGERPRINT` | C→reader，S；账号scope，不合并通用scope |
| `E_XHS_PROFILE_DIR`正式必需；`E_XHS_RUN_ID`根生成；`E_XHS_WINDOW_SIZE=1450,900` | root/C→session/launcher/BC，B/S/L；最小尺寸校验保持 |
| `E_XHS_POST_INTERACTION=none/E_XHS_ENRICH_CREATORS=1/E_XHS_KEEP_AUTHOR_DETAIL=1` | root/C→core/store，S/L；一次互动latch、作者详情净化（T14-C 已删除 `E_XHS_KEEP_AUTHOR_DETAIL`：根实现恒保存平台原值，无消费者；已登记 `AUTHORIZED_ENV_REMOVALS`） |
| `E_XHS_REPAIR=0/1/E_XHS_REPAIR_BATCH_SIZE=5/E_XHS_REPAIR_REPORT_PATH` | C→repair，B/F；batch正数，报告仍清理 |
| `E_XHS_INITIAL_SETTLE_SECONDS=12/INITIAL_SHELL_TIMEOUT_SECONDS=30/SEARCH_SHELL_TIMEOUT_SECONDS=30/RECOVERY_SHELL_TIMEOUT_SECONDS=60/NAVIGATION_DEADLINE_SECONDS=60`（各补E_XHS_） | settle/navigation父覆盖，三shell值继承operator；各操作L取值，导航至少5秒 |
| `E_XHS_LOGIN_WAIT_SECONDS=600`，C headed600否则0 | C→惰性manual budget，全轮共享；有限值范围0–600 |
| `E_XHS_QR_REFRESH_SECONDS=180/LOGIN_POLL_SECONDS=1/STABLE_LOGIN_SECONDS=5`（各补E_XHS_） | QR父覆盖180，下限180；poll最低0.2、stable不低于poll；login起点L读取，锁存后不刷新 |
| `E_XHS_NETWORK_WAIT_SECONDS=600/NETWORK_RETRY_MIN_SECONDS=2/NETWORK_RETRY_MAX_SECONDS=30/CREATOR_VERIFY_POLL_SECONDS=2`（各补E_XHS_） | C→网络恢复/作者等待，L；保持原预算包含关系 |
| `E_XHS_CREATOR_VERIFY_WAIT_SECONDS=600` | 父发无消费，T12删除；不得凭名称造第二个600秒预算 |
| `E_XHS_BATCH_CHECKPOINT_ENABLED`精确1，repair/no-import强制0；`E_XHS_BATCH_DATA_ROOT/E_XHS_BATCH_RESUME_SUMMARY` | root/C→publisher，每批L重新校验，非启动缓存 |
| `E_XHS_LEASE_DB/E_XHS_LEASE_ID/E_XHS_LEASE_OWNER_TOKEN` | root→C登记，B/L；token**秘密**；三者不转发worker |
| `E_XHS_RUNTIME_STATUS_AUTH_KEY_HEX` 32字节key的64位hex | root→C reporter，B后pop；**秘密**，worker不继承 |
| `E_XHS_RUN_SCOPED_LOGIN/E_XHS_STORAGE_STATE_PATH` | 已删除输入；XHS入口保持拒绝，不恢复兼容 |
| `PYTHONUNBUFFERED/PYTHONIOENCODING/UV_CACHE_DIR` | C setdefault=1/utf-8/集中缓存，B；继承值优先 |
| 子进程`HOME/PLAYWRIGHT_BROWSERS_PATH`；`PATH`、HTTPX env | RT/browser_runtime重定位子HOME、缓存存在才setdefault；PATH/HTTPX trust_env原样继承；不改当前shell HOME |

fork的MYSQL/REDIS/MONGODB/POSTGRES凭据族随无用DB/proxy导入退出；不复制默认密码到规格或配置对象。
首次日志/摘要/事件/manifest/JSONL持久写前净化；XHS详情URL内xsec_token仅在原必要URL通道传递，禁止普通配置dump。

### D3｜读取与生命周期：不可被新对象提前或刷新

| 状态／资源 | owner与精确时点（F→T不变） |
|---|---|
| 通用checkpoint | R:229准备job读；R:829收尾重新读核对本run；C:2951提交时新连接重新读旧前沿，不复用启动缓存 |
| 四站known集合 | U:180在每次search入口、关键词循环之前加载一次；web_posts平台ID/URL末段＋scope seen（通用另exclusion）＋resume文件；轮内集合增量，无每候选重查 |
| 并发可见性 | 上述多SELECT未显式建立跨表原子快照；加载后其他worker新提交不自动进入集合，不能宣称更强隔离 |
| Bili集合 | C:5066独立实现，行为Context启动前各读一次，不能强塞四站accumulator而改变错误路径 |
| source_keyword/crawler_type | 原ContextVar在每关键词/模式设置，store写入时消费；改显式WriteContext，不把首词缓存给后续所有词 |
| Cookie | WB/DY行为后refresh；ZH导航后、行为前refresh；XHS登录后/行为后/验证恢复后refresh；签名按每次请求取当前内存值 |
| HTTPX/TLS | client每次async with创建/关闭，verify在factory调用取值；连接数不等于BrowserContext数，不改池化策略 |
| 抖音/知乎签名资源 | 抖音选站加载signer时compile；知乎首次签名compile后缓存；XHS每调用Xhshow，Bili每轮WBI keys/每请求wts |
| XHS profile/lease | root LeaseGuard.prepare_runtime_session:1894创建空0700 session并claim；worker仅借用；root精确收束再删除、释放，不靠PID/TTL猜测 |
| XHS Chrome/Context/Page | launcher在Popen前置launch latch；一个Context；session持主Page与新页守卫，login/client仅借用；主Page死亡即失败 |
| XHS认证状态 | root按身份/序列/auth读取，接收单调时钟判存活；执行器heartbeat只做监督；root进度callback可提交批次，两者不能同名混用 |
| 执行state与批次 | 每阶段重新load；publisher每批重读state/plan/campaign/导出；root每批再读scope/hash/lease；不改成单次缓存 |
| 关闭/取消 | 原main/app_runner/CDP/父监督/root各收自己资源；取消不转候选空值，XHS先terminal证据再精确清理；保留既有部分清理错误吞并/优先级 |

### D4｜完整窄接口定义（T；仅文档，不创建stub）

以下使用Python 3.11语法描述完整类型。JSON仅是**既有wire的值域**，不是新万能配置或允许任意对象的context；
具体记录/事件字段由 E 表及唯一原serializer限定。`Mapping`参数不接收Page、Cookie、连接或异常对象。
选用小的结构类型，不造未定义的 `SelectedRecord/RunSummary/Result/Config` 类型占位。

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal, Mapping, Protocol, Sequence, TypeAlias
from playwright.async_api import BrowserContext, Page

JSON: TypeAlias = str | int | float | bool | None | list['JSON'] | dict[str, 'JSON']
Object: TypeAlias = dict[str, JSON]
Platform: TypeAlias = Literal['bilibili', 'weibo', 'douyin', 'zhihu', 'xhs']
Position: TypeAlias = int | str | None

@dataclass(frozen=True)
class GenericScope:
    job_id: int
    query_fingerprint: str

@dataclass(frozen=True)
class XhsScope:
    target_key: str
    account_id: str
    query_fingerprint: str

@dataclass(frozen=True)
class QueryInput:
    platform: Platform
    keyword: str
    source_options: Object  # 仅原source_query_options输出；不是整份params
    mode: Literal['search', 'detail']
    detail_targets: tuple[str, ...]  # 不dump带token URL

@dataclass(frozen=True)
class DiscoveryInput:
    scope: GenericScope | XhsScope | None
    start_page: int
    start_offset: int
    start_cursor: str
    top_refresh_max_pages: int
    source_exhausted: bool
    resume_identities: Path | None
    campaign_summary: Path | None

@dataclass(frozen=True)
class WriteContext:
    platform: Platform
    storage_key: str
    source_keyword: str
    crawler_type: Literal['search', 'detail']
    last_modify_ts: int  # writer按旧写出时点取毫秒时钟，不是平台发布时间

@dataclass(frozen=True)
class SourcePosition:
    source_page: Position = None
    source_offset: int | None = None
    source_cursor: Position = None
    next_cursor: Position = None
    source_has_more: bool | int | None = None
    raw_batch_count: int | None = None
    resume_page: Position = None
    resume_offset: int | None = None
    resume_cursor: Position = None
    batch_complete: bool = False
    discovery_phase: str = 'frontier'

class KnownCandidatesReader(Protocol):
    def load(self, platform: Platform, scope: GenericScope | XhsScope | None,
             resume_ids: Path | None) -> set[str]: ...

class LegacySearchEvents(Protocol):
    def append(self, event_type: str, details: Mapping[str, JSON]) -> None: ...

class XhsBatchPublisher(Protocol):
    def publish(self, details: Mapping[str, JSON]) -> None: ...

class CandidateDecisions(Protocol):
    @property
    def can_continue(self) -> bool: ...
    def begin_batch(self) -> None: ...
    def is_known(self, identity: str) -> bool: ...
    def consider(self, identity: str, *, valid: bool) -> bool: ...
    def skip_candidate_failure(self, identity: str, *, failure_scope: str,
        detail: str, error_code: str, attempts: int, retryable: bool | None = None,
        source_index: int | None = None, source_page: Position = None,
        source_offset: int | None = None, source_cursor: Position = None,
        discovery_phase: str = 'frontier') -> bool: ...
    def finish_batch(self, position: SourcePosition, *, count_stagnation: bool = True) -> bool: ...
    def mark_source_exhausted(self, detail: str, position: SourcePosition) -> None: ...
    def mark_runtime_failed(self, detail: str, position: SourcePosition) -> None: ...
    def summary(self) -> Object: ...
    def should_reseed_frontier(self, *, saved_source_exhausted: bool,
        refresh_has_more: bool | int | None, refresh_next_cursor: str | None,
        refresh_new_candidate_count: int) -> bool: ...  # 仅抖音调用，原U:450纯判定

class ImageStager(Protocol):
    def stage(self, *, platform_storage_key: str, platform_key: str, platform_post_id: str,
              source_key: str, assets: Sequence['ImageAsset']) -> list[Object]: ...
    def record_failure(self, row: Object) -> None: ...

class RecordSink(Protocol):
    async def write_content(self, raw: Mapping[str, JSON], context: WriteContext,
                            *, item_type: str = 'contents') -> None: ...

class HumanBehavior(Protocol):
    async def run(self, page: Page, *, platform: Platform) -> Object: ...

class BrowserBorrow(Protocol):
    @property
    def context(self) -> BrowserContext: ...
    @property
    def primary_page(self) -> Page: ...
    async def new_page(self) -> Page: ...  # XHS实现安装原守卫
    async def close_page(self, page: Page) -> None: ...

class RuntimeObserver(Protocol):
    def checkpoint(self, *, phase: str | None = None, network_state: str = 'unknown',
                   network_reason: str = '') -> bool: ...

class XhsSigner(Protocol):
    def sign(self, uri: str, data: dict[str, JSON] | str | None = None,
             cookie_str: str = '', method: str = 'POST') -> Object: ...

Clock: TypeAlias = Callable[[], float]
Sleep: TypeAlias = Callable[[float], Awaitable[None]]
Uniform: TypeAlias = Callable[[float, float], float]
RandInt: TypeAlias = Callable[[int, int], int]
CookieReader: TypeAlias = Callable[[], str]
RootBatchCommit: TypeAlias = Callable[[], None]
RecordProjector: TypeAlias = Callable[[Mapping[str, JSON], WriteContext], Object]
```

`SourcePosition`逐字段对应U:286 kwargs；不转换int0为False、不解释cursor、不把来源观测当批准前沿。
exhausted调用点必须显式传原`batch_complete=True`，failed与finish沿各自原值；禁止由一个公共默认改变原终态。
CandidateDecisions只迁原计数/判定与事件调用；`mark_source_exhausted`返回值不承担完成判据。
RecordSink装配时绑定本站纯RecordProjector；保留raw直到头像键和同记录重复URL均可识别：先清理完整payload，再投影、出口再次sanitize。
不得先丢头像键后只清投影；raw只限内存，禁止自动日志/repr。parser不持有sink、不做IO；写出owner在原store构造记录时点取last_modify_ts放入WriteContext，parser只使用该值。
不新增通用HttpTransport、统一Crawler/BrowserOwner大协议；各站client及原launcher具体函数沿原签名迁入。
静态options按D1/D2逐消费者构造局部冻结参数，接口不接受完整Namespace/env；动态CookieReader/Clock/Random与scope reader分别注入。
主稿旧 `SearchRequest/PageObservation/DetailObservation/AuthorObservation/ImageObservation/AdvanceDecision/ContentCommitResult/DiscoveryCommitResult`
不再作为待实现的另一套公共API：位置以SourcePosition具体化，其余沿用本站模型/现有wire与既有事务返回值。

| 精确函数/端口签名与实现 | 输入→输出、异常/取消、生产者→消费者、执行位置 |
|---|---|
| `entry.main(argv: Sequence[str] | None = None) -> int` | worker_inputs解析→选择本站；参数错误沿parser退出；外层根门禁不以0判成功；取消交RT/worker清理 |
| `KnownCandidatesReader.load(...) -> set[str]` | db/discovery_read实现；平台search调用；绑定只读能力，旧SQLite/文件错误策略见 E3；不暴露SQL连接 |
| `LegacySearchEvents.append(...) -> None` | application/events实现，worker/Bili执行器同步调用；无路径/旧捕获异常仍return；不增加strict默认 |
| `XhsBatchPublisher.publish(...) -> None` | 根BC.publish_batch绑定显式env映射实现，worker调用；阻塞至ACK，原60秒/0.25秒；失败由application按E:143旧前缀写终态再raise |
| `RootBatchCommit() -> None` | 根BC.BatchCheckpointCommitter.__call__，只root监督持有；SQLite事务/精确guard成功后写ACK；平台/observer无此能力 |
| `ImageStager.stage/record_failure` | artifacts/image_staging实现；四站原store_post_images/record_failure调用点；同步、整帖字节校验、manifest原子upsert；不拥有HTTP/晋升/DB |
| `RecordSink.write_content(...) -> None`（async） | records净化→artifacts/jsonl；每个store原await时点调用；序列化/文件错误传播；取消不当写成功，未await完不得publish |
| `HumanBehavior.run(...) -> Object`（async） | RT/behavior绑定evidence_path/profile；借Page→既有行为字典，含IO；写证据后判失败顺序不改 |
| `BrowserBorrow`四成员 | runtime/session生产，login/client消费；只同进程借用，无owner close/relaunch/lease释放权；Page死亡保持原异常 |
| `XhsSigner.sign(...) -> Object` | client调用，仅返回原x-s/x-t/x-s-common/x-b3-traceid四键且不强制转换值类型；data非dict仍走旧空dict签名分支，不改method/body；Cookie仅内存 |
| `fetch_bilibili_article_page(keyword: str, page: int, *, wbi_keys: tuple[str,str] | None = None, cookie_header: str = '') -> list[Object]` | 同执行器client同步urllib；原HTTP/业务错误，不空列表吞异常 |
| `fetch_bilibili_article_detail(post_id: str, cookie_header: str = '') -> Object`；`fetch_bilibili_follower_count(creator_id: str, cookie_header: str = '') -> int | None` | 原detail/follower异常及None保留；重试仍外层旧函数拥有，不给port再加重试 |
| `validate_formal_record(platform_key: str, record: Object, seen: set[str], *, allow_xhs_title_image_only: bool = False) -> Object` | records/formal同步纯判定；C:2773原字典字段原样，异常不降为空结果；worker应用规则可复用 |

内容提交保持现有签名，不新增“统一提交结果”结构。精确类型来自现有
`artifacts/image_materialization.py:MaterializedImage`，不是悬空模型；`Object`定义见上。
`materialize_formal_record_images(selected: list[Object], *, project_root: str | Path, media_root: str | Path, promote: bool, progress_callback: Callable[[], object] | None = None) -> tuple[Object, dict[str, list[MaterializedImage]], set[str]]`：
collection→artifacts，返回报告/按identity图片/失败身份集合；路径默认由入口按原paths解析后显式传递。
`import_valid_records_with_media_rollback(summary: Object, selected: list[Object], db_path: Path, *, materialized_images_by_identity: dict[str, list[MaterializedImage]], image_materialization: Object, project_root: str | Path, media_root: str | Path, progress_callback: Callable[[], object] | None = None) -> Object`：
collection→db/content，保留 `FormalImportBeforeCommitError` 与提交结果不确定的分流，取消按原事务/引用核验处理。
这两个progress_callback是执行器认证心跳；不是XL:2193承担发现提交的root callback，名字同形不合并权限。
staging继续用C1原关键字接口。ImageAsset从M image_manifest:48迁入artifacts/image_staging，完整字段为
`source_index:int, source_asset_key:str, source_url:str, content:bytes, attempts:int=1, http_status:int=200`；只在内存传字节。
`stage_post_images(*, save_data_root: Path, platform_storage_key: str, platform_key: str, platform_post_id: str, source_key: str, assets: Sequence[ImageAsset]) -> list[Object]`；
`failed_manifest_row(*, platform_key: str, platform_post_id: str, source_key: str, source_index: int, source_asset_key: str, source_url: str, attempts: int, error_code: str, http_status: int | None = None) -> Object`。
均同步；前者负责整帖字节校验/写出，后者构造失败行交同一writer写出；保留原图片/路径/文件错误，不拥有HTTP、晋升或数据库能力。

## E｜wire、提交顺序与错误兼容

### E1｜既有字段与序列化责任

所有字段为F，T按旧serializer输出，不加schema版本、不把SDK模型dump作wire。字段可省略/为null/为0的规则逐站保留。

| wire／生产者→消费者 | 必须保持的字段、语义与格式 |
|---|---|
| 五站内容JSONL；各store投影→C:`collect_formal_records`:3333/`row_for_record`:2660 | 本站ID/title/body/time/author/metrics/image字段原名；WB的mblog、DY的aweme、ZH的answer/article、Bili的article不可统称同一原响应 |
| 微博 SW:update_weibo_note:140→根 | `note_id,content,create_time,create_date_time,liked_count,comments_count,shared_count,last_modify_ts,note_url,image_list,image_count,image_list_source,image_assets,creator_hash,nickname,followers_count,fans_count,followers_observed,author_followers_source,source_keyword,content_detail_status,content_detail_source`；保留字符串/数值及缺值投影，不强制转成新通用模型 |
| 抖音 SD:update_douyin_aweme:287→根 | `aweme_id,aweme_type,title,desc,create_time,creator_hash,nickname,followers_count,fans_count,followers_observed,following_count,aweme_count,author_liked_count,author_followers_source,author_followers_zero_suspicious,liked_count,collected_count,comment_count,share_count,last_modify_ts,aweme_url,cover_url,video_download_url,music_download_url,note_download_url,source_keyword`；JSONL加`content_detail_status/content_detail_source`，图片启用才加`image_assets/image_list_source`；保留旧媒体字段名不授权视频请求 |
| 知乎 SZ:update_zhihu_content:108 / M/model/m_zhihu.py:ZhihuContent:25→根 | `content_id,content_type,content_text,content_url,question_id,title,desc,created_time,updated_time,voteup_count,comment_count,image_list,image_count,image_list_source,image_assets,content_detail_status,content_detail_source,source_keyword,creator_hash,creator_url_token,user_nickname,author_profile_url,followers_count,followers_observed,author_followers_source,following_count,author_desc,verified_text,last_modify_ts`；model内avatar_url净化后不落盘 |
| Bili C:normalize_bilibili_article_record:4470 / hydrate:4861→根 | 初始`id,content_id,content_type,title,desc,search_desc,search_excerpt_length,search_preview_urls,search_preview_count,content_text,content_detail_status,content_detail_source,content_images_detail_status,content_url,image_urls,source_keyword,created_time,published_at,liked_count,comment_count,view_count,nickname,user_id,raw_bilibili_type`；hydrate更新正文/标题/图并增`content_length,content_detail_attempts,content_detail_retry_wait_seconds,content_detail_pacing_wait_seconds,detail_image_urls,detail_image_count,detail_image_sources,detail_opus_observed,detail_opus_paragraph_count,detail_content_image_token_count,content_detail_evidence`；后续粉丝/图片结果仍按C:5306/5382原enrichment，不覆盖发布时间 |
| XHS SX:237–292→根字段/媒体门禁 | `note_id,type,title,desc,video_url,time,last_update_time,creator_hash,user_id,nickname,author_profile_url,author_desc,gender,ip_location,fans,fans_count,followers_count,followers_observed,author_followers_source,follows,following_count,note_count,interaction_count,creator_profile_json,liked_count,collected_count,comment_count,share_count,image_list,image_assets,image_list_source,tag_list,last_modify_ts,note_url,source_keyword,xsec_token,content_detail_status,content_detail_source`；avatar_url内存证据不落盘 |
| `image_assets` SX:68、各站parser→下载/根复验 | `url,source_index,source_asset_key`；图序/稳定键不可重排；creator_profile_json保持字符串及现有递归净化规则 |
| manifest；M image_manifest:260/373→根image_manifest及BC:52 | schema_version=1；`platform_key,platform_post_id,image_role,source_index,source_key,source_asset_key,source_url,fetch_status,attempts,http_status,staging_path,size_bytes,mime_type,width,height,sha256,error_code`；失败字节字段null，排序/紧凑JSON/末尾换行保持 |
| execution state；ES:create:77→阶段控制/BC | `schema_version,run_id,job_key,site_key,job_kind,status,created_at,updated_at,plan,plan_sha256,frozen_inputs,steps,events`；event=`at,type,details`，不能由stdout替代 |
| adaptive_batch_completed；U:326–350→分页汇总/BC/XD | `platform,batch_no,candidate_count,valid_new_count,valid_existing_count,batch_new_count,batch_candidate_identity_count,stagnant_batches,stagnation_basis,stop_reason,source_page,source_offset,source_cursor,next_cursor,source_has_more,raw_batch_count,resume_page,resume_offset,resume_cursor,batch_complete,discovery_phase,candidate_identities` |
| adaptive_search_stopped；U:356/summary→根完成门禁 | 上述累计位置/身份及`pages_fetched,stop_detail,skipped_candidate_count,skipped_candidate_failures`；必须明确source_exhausted，不用数量或停滞代替 |
| candidate_skipped；U:234→报告/安全前沿 | `platform,identity,failure_scope,detail,error_code,attempts,source_index,source_page,source_offset,source_cursor,discovery_phase,retryable`；不把运行级失败降为候选skip |
| xhs_runtime_terminal；E:161/XK:1012→C:6428/X:556 | `phase,failure_type,stop_reason,stop_detail,retryable`与登录既有terminal_context；精确子类型优先，不被泛化timeout覆盖 |
| XHS batch summary；BC:151→root/续轮C | `keyword,completion_mode,import_completion_met,import_result,formal_validation,records,pagination_evidence,batch_checkpoint`；import=false、reason=batch_checkpoint_only，records历史前缀原样＋本轮incomplete/ok=false及output jsonl_files/image_manifest_paths |
| batch_checkpoint；BC:171→committer | `run_id,account_id,target_key,query_fingerprint,sequence,files:[{path,sha256}]`；summary命名`batch-<六位sequence>.summary.json`，snapshot=`<源名>.batch-<六位sequence>.snapshot` |
| pointer与ACK；BC:185/307 | 相同对象`{summary,sha256}`；文件`batch_checkpoint.json`与`batch_checkpoint_ack.json`；必须精确相等，不用旧ACK确认新pointer |
| discovery plan；XD:62→X/C/worker | `target_key,account_id,keyword,query_fingerprint,checkpoint_found,resume_page,resume_search_id,source_exhausted,top_refresh_max_pages,campaign_summary_path,checkpoint_before` |
| runtime_status与claim；xhs/runtime:41–89→root | status schema2含run/account/lease/writer身份/sequence/heartbeat/phase/network/auth_tag；公开仅`writer_role,sequence,heartbeat_at,phase,network_state,network_reason`；claim schema1含`run_id,account_id,lease_id,owner_token_sha256,auth_tag`，不输出secret |
| repair report；E:316→C/Q | `schema_version,platform,target_count,batch_size,batch_count,batches,candidate_failures,successful_ids,runtime_blocker,successful_count,failed_count`；fallback metrics标`existing_web_posts_metric`，fresh observed零不能被说明为缺失 |
| 最终摘要／路径；C:7080–7137、X:1516–1553 | 原summary/report/batch_dir/status/import_completion_met/failure_reason包络；终态token=`token,outcome,committed,finalization_confirmed`；job_kind=mediacrawler_search及mediacrawler_runs等路径不改 |

WB `_first_present`保留0；ZH模型默认0只有source/observed有效才算观察；Bili粉丝缺失None不变0；
XHS合法空和unknown依旧分开；DY当前部分truthy默认/来源标记的差异在H列风险，不能用公共缺值helper抹平。
原始平台时间不补抓取时钟，last_modify_ts不变成published_at；Bili hydrate不覆盖搜索发帖时间。
签名前的URI、query顺序/编码、JSON紧凑body、form、空键及Cookie读取时点原样，禁止通过新模型dump重编码。
DY client:306用urlencode生成签名输入、get:413交HTTPX params发送；二者分别与旧trace比较，不能未经验证声称字节相等，也不能为统一二者而改编码。

### E2｜XHS publish→DiscoveryCommit→ACK→ContentCommit

1. root冻结target/account/query与campaign计划、取得精确lease并创建空session；gate登记后启动执行器及worker（X:1037/1103，C:1916）。
2. worker单会话扫码；search入口读一次known；每页详情→作者→图片staging→JSONL，全部store await结束（XK:1934–2167）。
3. application生成批次details、同步调用legacy事件sink，再**显式**调用publisher；发布仅enabled=1、platform=xhs、batch_complete is True、source_has_more is not False（U:350，BC:89–96）。
4. publisher重读running state、最后event details、run及冻结campaign；有效计数>0无内容文件即失败，已知空refresh可无文件；不能以传入内存event替代落盘检查（BC:93–123）。
5. 独占xb写snapshot并fsync文件/目录；合并冻结campaign records，写summary再pointer并同步目录；等待精确ACK，60秒上限、0.25秒轮询（BC:124–189）。
6. root callback先检查signal，再查pointer/hash/sequence/四元scope；重读state/campaign及事件，验证page+1/cursor、inventory、路径/size/hash（BC:210–272）。
7. root BEGIN IMMEDIATE中重读lease_id+owner_token+run并再查signal；同事务写checkpoint/seen/campaign/审计event；事务成功退出才写ACK和fsync，最后更新last_sequence（BC:274–309）。
8. worker见ACK继续；来源耗尽另发stop。退出后执行器合并campaign累计records，在媒体锁内复验/晋升/内容SQLite事务；root再验证child结果、终态事务、状态/临时摘要→精确清理→最终摘要（C:6802/7016/7098，X:1236/1381，XT:309–378）。

BC snapshot复验不是完整图片解码/权威整帖/SQLite内容门禁；ACK先于最终内容提交，不能要求等ContentCommit。
合法先前ACK不因后续中断回滚；operator_interrupt禁止新增终态发现推进，不抹除旧commit。
完整批次规则只限制批次ACK；XD:243/276普通终态失败可能保存event中已处理ID，不可写成“未完整尾批绝不seen”。
未处理ID仍不可越过，页中断恢复当前页。BC的`is False`与search接受数值0的区别保留，不做静默布尔规范化。

### E3｜错误时点与失败表：首期默认兼容，不全局fail-closed

| 当前F边界／错误 | 当前结果与T映射 | 验证责任 |
|---|---|---|
| U:28外层SQLite/OSError | rows=[]，随后仍可合并resume；内部seen/exclusion查询ValueError/SQLite错误仅清该子集合，已有web_posts保留 | reader保持相同分支，不改全部严格；F09 |
| U:117 resume OSError/JSONDecodeError/TypeError | resume=[]，DB集合保留；非该捕获集错误不擅自吞掉 | reader故障矩阵；F09 |
| U:131无state路径 | 直接return，无event；不是持久化成功回执 | LegacySearchEvents同语义；F09 |
| U:131读/写/replace的OSError、JSONDecodeError、TypeError | 直接return；临时替换不等于fsync；不是所有异常均吞掉 | 不默认新strict、不添加重试；F09 |
| ES:210严格append／阶段状态失败 | 沿原FrozenExecutionState异常传播；与上行不同出口 | core/execution_state保持；F02/F06 |
| E:143原append后BC:96重读 | event未持久、状态/run/campaign不匹配即失败；publisher非前缀异常包装`xhs_batch_checkpoint_<type>`，尝试legacy terminal事件后raise RuntimeError | 显式应用调用保留错误包装与时点；F02 |
| BC snapshot/summary/pointer失败 | 部分快照可能已存在，无合法pointer不提交；xb冲突不是通用幂等重试 | 禁扫描孤立summary猜提交；F02 |
| root事务前/中失败 | 无ACK，事务回滚；旧安全前沿保留 | F02/F06 |
| DB commit后ACK写失败 | 发现提交已有效；再次callback可upsert，seen唯一；审计event不保证exactly-once | 不回滚合法提交；F02 |
| ACK写成功但目录fsync失败 | child可能已读ACK，DB已提交；不能宣称掉电级ACK确定持久 | 保留证据/既有恢复；F02 |
| ACK后ContentCommit前死亡 | campaign snapshot仍是累计内容；新轮扫码、known跳过已处理ID但合并历史records；snapshot缺失/篡改拒绝恢复 | F02/F05 |
| WB request vs 长文详情 | request 5次/3秒最终可能RetryError；get_note_info_by_id直连一次，WeiboFullTextFetchError默认attempts=3不是3次调用证据 | 记录实际调用trace及元数据，保持差异；F11/F09 |
| ZH request/详情 | 3次/1秒/reraise；401/403/429分类，404空dict保留；request_failed/parse_failed不混 | F13/F09 |
| DY request/detail | 无统一Tenacity；搜索blocked/含糊空运行失败；detail部分宽异常→None保留，不能解释为搜索耗尽 | F12/F09 |
| XHS request/detail/恢复 | client适用3次/1秒，NotFound/IPBlock/PlatformRuntime排除；外层已有详情/网络/人工budget包含关系原样 | 允许原固定嵌套次数；不得新增放大；F15/F09 |
| Bili详情/粉丝/图片 | 原适用最多3次，部分运行级码先经旧详情重试再失败；None/候选skip/运行blocked保留 | 不统一“运行错误一次返回”；F14/F09 |
| WB/ZH搜索过滤后空 | 现行可触发耗尽；不能冒充已核验原始平台has_more终态 | 保留基线，原始空/非目标/解析失败差异另卡；F11/F13 |
| QR无参数sys.exit、app_runner部分cleanup错误 | 退出可能为0／清理仅打印；最终仍由字段/来源/持久化门禁裁决 | 不拿exit=0成功，不借抽象改退出码；F07/F06 |
| 内容事务commit前确定失败 | 仅回滚本轮新增且经引用核验允许删除的媒体，复用文件不删 | F05 |
| 内容commit已发生/不确定 | 不盲删可能被SQLite引用的文件，保留既有探测与证据 | F05/F06 |
| XT:finish:274诊断持久化 | 返回结果与磁盘provisional/pending摘要可能不同，#2未覆盖 | 独立风险R01；不得声称已解决 |

未来如果确需严格控制端口，在另行批准的行为变更中定义 `StrictSearchEvents.append(...) -> None`（签名与legacy相同），
仅在显式选择的新调用点传播写错误，并分别验收异常类别、失败时点、恢复前沿；首期装配不选择它，也不作为本次设计阻塞。
重试原则是**不新增次数放大**：旧库内/外层已存在的嵌套次数、等待及最终异常先等价保持；独立预算重构另案。

## F｜测试迁移责任与CI交接

本节是T任务，不表示本轮执行或已存在新测试。表中root=`tests/`，fork=`M/tests/`；新路径均相对root。
保留业务断言，修改导入/fixture/patch接缝；旧hook结构断言先有显式出口替代再删除。

| ID | 旧文件/节点或行锚点（F）→新文件（T） | 必须保留/补齐的保护语义 |
|---|---|---|
| F01 | root `test_discovery_checkpoints.py:49/461/651`、`test_mediacrawler_pagination.py:223/504/538`；fork `test_trippostcollect_adaptive.py` → `application/test_discovery.py` | 原FP排除/ canonical bytes、一次读取、scope/exclusion、游标三元组、失败前沿、无stop不完成 |
| F02 | root `test_xhs_batch_checkpoint.py::test_exporter_hook_publishes_only_after_durable_event`、`test_complete_batch_survives_root_loss_and_mutable_export_tail`、`test_empty_refresh_commits_history_without_creating_export_files`、`test_ack_failure_after_db_commit_is_idempotent`、`test_exporter_checkpoint_failure_keeps_precise_terminal_evidence` → 原文件改显式接缝 | append→publish、snapshot、空refresh、DB先ACK、故障前缀、旧ACK与内容提交分离；补fsync失败及审计重复边界 |
| F03 | root `test_full_content_contract.py::test_observed_zero_followers_is_valid:159`、`test_incomplete_follower_evidence_is_invalid:184`、`test_missing_platform_time_cannot_be_replaced_by_capture_time:200` → `records/test_formal.py` | 固定五站，真实0/source/observed、时间、正文状态、缺键/null/合法空分别断言 |
| F04 | root `test_author_avatar_sanitization.py:23/146/184` → `records/test_serialization.py` | 完整payload头像证据到投影、首次JSONL/event/log/manifest清理及同URL重复；不只测新writer名 |
| F05 | fork `test_image_download_retry.py:11/34/73/118`、`test_image_staging_errors.py:17/26/33`、`test_image_client_http_classification.py` → `artifacts/test_staging.py`；root `test_mediacrawler_import.py::test_postcommit_interrupt_preserves_sqlite_referenced_media`及`test_interrupt_before_batch_commit_rolls_back_rows_and_media` → `application/test_content_commit.py` | 重试、真实格式/字节、失败行、整帖拒绝、晋升后失败、commit确定性与引用保护 |
| F06 | root `test_xhs_leases.py`、`test_xhs_terminalizer.py`、`test_xhs_runtime_status.py`、`test_xhs_supervision.py`、`test_mediacrawler_process_watchdog.py` → 原root职责文件改导入；fork `test_cdp_browser.py/test_cdp_browser_lifecycle.py` → `runtime/`同名文件 | #1/#2修复语义、真实信号/gate/身份/精确清理、unknown/scan_error不释放；component/OS/socket分组保留 |
| F07 | root `test_login_warmup.py::test_weibo_login_accepts_current_mobile_session:81`、`test_mediacrawler_behavior.py` → `application/test_warmup.py`、`runtime/test_behavior.py`；本站login用例随站迁 | Cookie刷新次序、辅助driver不推广、既有fallback范围、XHS单会话/刷新锁存/页守卫/共享budget |
| F08 | root `test_post_detail_repair.py:54/413/505`、`test_residual_detail_fallbacks.py:20/51`、`test_xhs_post_repair.py`、`test_bilibili_article_repair.py:711` → `application/test_repair.py`、`platforms/test_repair_fallbacks.py`及原XHS/Bili根文件 | 精确ID、禁发现写、同Page回退、部分成功、XHS signal终态优先、fresh零值不覆盖 |
| F09 | **拟新增** `platforms/test_migration_contract.py`（由各旧client/accumulator测试提取场景） | 同clock/random/Page值比较method/URL/query/body/header/sign输入、异常/次数/等待trace、容错时点、JSONL字节与写出时点；未匹配请求失败，不连真实端点 |
| F10 | root `test_run_ids.py` installation职责、CI `run_matrix.py:15/26/152` → `test_platform_package_resources.py`及更新CI清单；拟新增 `runtime/test_page_readiness.py` 和 `application/test_page_evidence.py` | wheel/sdist仓库外资源、选站懒加载、无fork/cwd/PYTHONPATH补齐、Node桥、SQL/契约、辅助CLI、未选平台/DB不导入；补页面证据导航/就绪和Cookie清理边界，本轮无专项执行证据 |
| F11 | fork `test_weibo_empty_search.py::test_search_empty_message_is_returned_as_empty_page:30`、`test_weibo_image_download.py::test_specified_detail_downloads_images_before_store:79`及`:378/400/435/450`、`test_weibo_store.py:98`、`test_weibo_no_user_info.py` → `platforms/weibo/`同名文件 | 过滤空页、长短文正文来源、阻断不seen、pid图序、无头像/无多余作者请求 |
| F12 | fork `test_douyin_search_safety.py::test_search_request_uses_current_single_column_contract:326`、`test_browser_search_response_is_reused_before_duplicate_api_request:356`、`test_ambiguous_empty_first_page_is_runtime_failure:564`、`test_deep_frontier_rebinds_to_current_refresh_session_cursor:610`；`test_douyin_image_only.py:54/94`、store/no_user_info → `platforms/douyin/`同名文件 | 请求含空from_group_id、响应复用、健康后页证据、offset/search_id、图文过滤、URI身份 |
| F13 | fork `test_zhihu_search_detail.py:24/57/82/140/157/190/229`、`test_zhihu_detail_images.py:72/129`、`test_zhihu_image_download.py:37/124/153/212` → `platforms/zhihu/`同名文件 | 精确entity/正文块、搜索作者合并、纯快分支、request/parse区别、单semaphore、图片session |
| F14 | root `test_bilibili_formal_route.py:27`、`test_bilibili_article_detail.py:66/83/182/234/260/421`、`test_bilibili_image_materialization.py:121` → `platforms/bilibili/`同名文件，根保留路由集成 | 根article不替fork video、摘要不当正文、详情重试/粉丝skip、顺序/Header与known提前跳过 |
| F15 | fork `test_xhs_{core_access_error,creator_enrichment,discovery_memory,image_download,login_contract,manual_wait_budget,media_policy,midrun_login_recovery,network_recovery,popup_guard,qrcode_login,qrcode_preview,raw_response_errors,shutdown_error_priority,store_provenance}.py` → `platforms/xhs/`同名文件 | 所有原node与参数化语义保留；补XHS资源/reader/read一次/签名trace；根租约/终态/ACK测试不搬成平台测试 |

T00实施起步保存机器可读的**旧node→新node→保护语义→lane→前置卡**台账（目标 `tests/adapter_migration_inventory.json`）。
旧节点来自固定F/M收集，不以报告缩写或省略号冒充node；拆/合节点登记全部来源与新断言位置。
每站实现时同时迁测试，F09新增场景按站参数化，不能等旧树删除后再补测试归属。
`scripts/ci/run_matrix.py:FORK_OFFLINE_TESTS:15/FORK_EXPECTED_TESTS:26` 的32文件417项是旧基线，不能永久跑已删除路径（T14 已执行：T14-B2 移植剩余离线用例后清单清空，T14-C 删除 fork 后 fork lane 不再运行，#64 删除 FORK 常量与 fork lane 代码）。
T12更新root/worker组、静态导入清单、支持插件/fixture隔离和两版本计数基准；删除旧Bili视频import，新增根article＋五站选站装配验收。
迁移后的计数由职责台账与实际collect核对，不机械固定417；缺测、空收集、skip/xfail/xpass、导入失败不能冒充通过。
沿用[testing.md](testing.md)现有pytest/marker/lane/托管OS机制；届时同批更新其原fork引用，不把本轮限制变成项目永久规则。
不需要本期引入外部dev包；HTTPX MockTransport、已有fixture/AST检查先用。Import Linter/RESPX仅确有缺口时独立dev任务，不能顶替保护语义。

## G｜可执行任务卡与分层门禁

共同禁止项：不换签名/HTTP/driver，不改wire/FP/数据schema，不新增逐页RPC/统一大循环，不停用未迁站，不改生产checkpoint/media/历史证据。
每卡验收使用F责任；“回退”仅代码/装配回退，不回滚合法DB提交或ACK。以下均待实现授权，本轮不执行。
每张卡对应一个 GitHub issue，issue 正文是可逐条勾选的执行清单（改哪些文件、C8 中哪些符号、跑哪些测试、删除前核对什么），
与本表冲突时以本表和 C8 为准并同步修正 issue。卡内“C8 符号”指附录中卡号列等于该卡的全部行。

| 卡／对应主稿阶段 | 文件与符号、交付产物 | 前置／禁止项 | 检查与删除条件／回退 |
|---|---|---|---|
| T00／P00后启动 | 在实施基线重跑C8枚举并解释差异；生成F旧node台账、D CLI/env机器展开；确认C8/X11及退出切片“T00核”项 | P00-01–08复核＋实现授权；不是首次建立迁移闭包，不重跑外部选型 | 对新实施基线逐行/节点核对并解释漂移；只撤回准备补丁，不删源码 |
| T01／P01 | **根依赖先行**：迁入代码所需包按原 worker 环境精确版本写入根 pyproject/uv.lock，根 venv 成为唯一运行环境；core/resources、paths集中常量；C.ensure_prerequisites/profile_dir_for、W.main_async；JS/LICENSE 包内资源；台账冻结于基线并新增 `progress` 迁移进度 | T00；不读/移真实profile；不升级已锁版本 | F10资源路径/旧接口兼容；`symbols --check`、`inputs --check`、`progress` 无 missing；fork libs 在各站切换读取前与包内资源逐字节相同（测试断言），T06/T07/T09 切换后由 T14 删除（T14 已执行）；回退资源定位 |
| T02／P01 | P/entry、worker_inputs、RT/browser/launcher/worker/process；替换E.main/M.main装配；**worker 改用根解释器 `sys.executable -P -m trippostcollect.platforms.entry`，不再 `uv run`，cwd 为项目根**；分两个 PR：A＝入口/输入/worker/process 及 fork 去 cwd 与退出切片延迟导入，B＝browser/launcher/login_helpers 及 41 个 fork 浏览器测试迁根 | T01；不全注册、不重写关闭 | F06/F10；产物选站启动/清理trace；新轮命令冻结新入口，旧轮仍用旧桥；回退整个选站入口 |
| T03／P02 | D1–D3输入切片；db/discovery_read、A/candidates；scheduler/XHS scope保持 | T02；不每候选重查、不收紧容错、不全量hash | F01/F09逐读点、0/空、错误集合；产物读点表；所有调用注入后删对应env/config读；单接缝回退 |
| T04／P02 | records/formal、sanitization出口、artifacts/jsonl/staging/evidence、A/events/failures/policy、RT行为；显式publish接缝 | T03；不将legacy事件默认strict、不改变重试层/分类/冷却 | F02–F05/F07/F09及原failure_classifier/crawl_policy用例；产物wire/事件/IO顺序差分；新出口负例先通过再删hook；出口装配回退 |
| T05／P01–P03 | C2全部符号→微博目标及F11同批测试 | T04；长文直连与repair fallback范围保持 | F11/F08/F09/F07；产物完整微博fixture trace；search/detail/warmup全部切换才删旧切片；整站入口回退 |
| T06／P03 | C3全部符号→抖音目标、JS/slider/MEMORY及F12 | T04；不改空首屏/三元组、不新增签名算法 | F12/F08/F09/F10；产物body/sign/回建trace；条件登录/详情/辅助引用全迁才删旧链；整站回退 |
| T07／P03 | C4全部符号→知乎目标及F13 | T04；不变404/重试、不把辅助Patchright当正式driver | F13/F07/F09/F10；产物精确entity与Cookie/签名trace；warmup/诊断/repair全迁才删；整站回退 |
| T08／P03 | C5全部符号→Bili目标，B历史修复请求接缝 | T04；仍执行器、Context先关闭、urllib不换 | F14/F01/F05/F09；产物article调用顺序；所有辅助反向import解除后删C旧函数；article接缝回退 |
| T09／P04 | C6全部平台符号＋根runner/repair薄化；原XL/XT/BC/XD仍根所有 | T04及公共链稳定；不第二浏览器/换号/改ACK顺序 | F15/F02/F06/F08/F09；产物生命周期/故障窗口trace；根worker协同验收后删XHS hooks；整条XHS回退，保留已ACK证据 |
| T10／P03–P04 | C7两warmup、三类repair、B晋升/监督、benchmark/page evidence及readiness/Cookie辅助；获迁入口scripts仅转发 | T05–T09；不扩大修复范围，不把诊断算正式完成 | F07/F08/F10并补页面证据就绪/Cookie场景；产物CLI对照和包内依赖图；全调用方迁完才删脚本业务，保留外部名字；逐入口回退 |
| T11／P04 | C8卡号T11全部符号：C的收集/门禁/发现提交/内容事务/摘要/运行阻断→collection、reporting、failures、artifacts/formal_images、db/content | T05–T10；不合并ContentCommit/DiscoveryCommit，不改提交不确定清理 | F01/F02/F05/F06临时SQLite/文件故障；产物事务顺序报告；旧函数无引用且保护语义迁完才删；提交接缝回退 |
| T12／P05 | 资源构建、CI run_matrix/FORK清单、tests台账、testing.md目标分组；按C8“退出切片的调用点切断”表删除全部保留代码中的退出分支（依赖合并与解释器切换已前移至 T01/T02） | T01–T11；不升级版本、不手工双份资源、不丢辅助依赖 | F10＋全部F；产物wheel/sdist、安装清单、旧新node/lane对账；静态/动态引用与干净安装全过才解除旧环境依赖；回退构建和新轮入口 |
| T13／P05 | 五站整合矩阵、component/socket/installation/托管OS双版本、离线故障差分 | T12；单站/总通过数不可替代五站责任，缺能力不得skip | 五站全部F覆盖、原#1/#2保持、静态依赖和资源无悬挂；产物分基线/lane报告；失败停当前阶段保留旧链 |
| T14／P06 | 旧轮清点、安全终止/完成证据、受影响权威文档同批切换，最后删M/E私有桥及无用registry/DB/GUI | T13＋正式窗口及必要治理授权；不能改半轮冻结命令/删历史数据 | 新轮冻结-m命令；旧轮无存活进程/租约依赖；checkpoint/media/历史产物可读；产物删除审计与切换报告；仅回退旧代码/环境，不改已提交业务状态 |

T00 已交付（2026-09-29，基线见 `docs/adapter-ledger/baseline.json`）：
- 工具：`scripts/dev/adapter_ledger.py`（子命令 `baseline`、`symbols`、`inputs`、`tests --source <临时副本>`、`make-source`、`all`），
  规则数据 `scripts/dev/adapter_ledger_rules.py`。`symbols --check`、`inputs --check` 用于每张后续卡的开工与收尾核对。
- 产物：`docs/adapter-ledger/{baseline,symbols,inputs,tests}.json`，C8 附录由 `symbols` 渲染。`tests.json` 记录 root 1002 个节点
  （component 949、os 51、socket 1、installation 1）与 fork 417 个节点，每个节点带目标文件、F 责任与卡号；只在临时源码副本中收集。
- 结论：X7 两处 PLATFORMS 对共有平台代号一致（dy/zhihu/wb/bili，W 不含 xhs），可按 T01 合并；X11 `repair_runtime_stop_reason`
  保留并随 T10 迁入 `application/repair.py`，其 2 个测试不改；三站 `IPBlockError`/`ForbiddenError` 与 `recv_sms.py` 在闭包内无引用，确认退出。
- 后续卡开工前运行 `symbols --check` 与 `inputs --check`；源码改动后先更新规则或迁移结果再提交，不手改 JSON。
- T14 已执行：fork gitlink 删除后台账冻结，#64 删除生成代码与 `tests`、`make-source` 子命令，`symbols`、`inputs`、`baseline`、`all` 只接受 `--check`；
  `symbols/inputs/baseline --check` 只做不访问 Git 对象库的冻结自检（登记散列、C8 附录由 JSON 逐字节重现、规则与冻结行一致）。

T01/T12确定的资源与安装策略（T）：JS和LICENSE迁入上述resources目录成为单一源码真源，旧地址过渡只委托resource reader，不留手工双份。
`db/{source_platforms,web_posts,ctf_captures,crawl_scheduler,xhs_control}.sql` 与必要 `docs/formal-crawl-contract.md`
继续以仓库现址为唯一编辑真源；目标根 `build_support.py:BuildPy`（setuptools build_py子类，pyproject cmdclass登记）从显式白名单复制到build_lib内
`trippostcollect/resources/sql/`、`resources/contracts/`，校验源/产物SHA；生成副本不进src/不允许手改。
目标 `MANIFEST.in` 使sdist包含这些真源及构建模块，wheel通过package_data包含生成资源；构建不下载fork、不读取运行状态。
`core/resources.py` 用importlib.resources读取；需路径时as_file覆盖完整使用寿命；根SQL/契约调用改用资源入口，运行DB/媒体仍走core.paths。
源码checkout的现有core.paths常量兼容；仓库外入口明确要求工作根（现有TRIPPOST_PROJECT_ROOT），不把site-packages上三级当运行根。
非XHS通用profile新目标固定为paths集中定义的 `data/runtime/platform_sessions/<platform>/profile` 与同目录 `trippostcollect_cookie_snapshot.json`（沿用现名，不改文件名）；
T14在旧轮结束且独占资源后按清单迁移/校验，再切新轮；XHS仍 `XHS_SESSION_ROOT` 每轮空session，不迁持久登录态。
T14 已执行：T14-A 起运行期只读写 `platform_sessions`，删除批只去掉“旧 fork 目录存在而新目录不存在”的对照检查，`.partial` 残留检查保留（错误码仍为 `platform_session_migration_required`）；旧目录处置见[运维手册](operations-runbook.md)。
历史媒体/manifest相对路径不改；`FONT_PATH/STOP_WORDS_FILE`只属关闭词云，字体/词云文件不列运行安装资源。
依赖与解释器前移（2026-09-29 用户确认）：T01 把迁入代码的直接依赖按原 worker 环境精确版本写入根 pyproject/uv.lock，根 venv 成为唯一运行环境；T02 起 worker 用根解释器运行新入口，禁PYTHONPATH/cwd伪补齐。上游 DB/GUI/词云等退出切片的依赖不进入根。
T02 过渡装载（2026-09-29 用户确认）：未迁站仍是 fork 顶层包，`platforms/_fork_bridge.py` 是唯一把 fork 目录与 `scripts/` 显式插入 `sys.path` 的位置，
worker 以 `-P` 启动使 cwd 不入路径；fork 子模块同批去除 cwd 相对路径（JS/stealth 经 core.resources，profile/temp_image 经 core.paths 取与原位置一致的绝对路径；T14 后 profile 只在 `platform_sessions`，滑块临时图在 `data/runtime/douyin_slider_images`），
并把 store/var/proxy/词云对 sqlalchemy、aiomysql、motor、redis、jieba、matplotlib、wordcloud 的顶层导入改为用到时导入（不删代码、不改分支）。
`execution_state` 前移至 `core/execution_state.py`（原属 T04），避免 runtime/process 反向导入 scripts。各站迁完且 E 删除后于 T12/T14 删除该过渡模块（T14 已执行：`scripts/execution_state.py` 与 `_fork_bridge.py` 已删除）。
根已有Scrapling/Patchright辅助依赖保留；Node/execjs校验保留。所有收编保留版权头、原许可、固定SHA及变更标识。

| 门禁 | 必需证据 | 本轮状态 |
|---|---|---|
| 设计就绪D | P00-01–08产物与复核；C0类别、C1–C7与C8全量符号闭包、D/E读取/错误、F/G任务及回退无未处置差异 | **静态部分已满足（2026-09-28）**：C8 1234定义全部有处置，锚点/CLI/env程序化核对通过；实施前T00须在实施基线重跑 |
| 实施回归I | T00–T13产物；五站离线差分、适用临时事务/真实OS、完整wheel/sdist安装、测试台账和懒加载引用检查；R04若证实违反正式边界，独立修正获准并验证后才能通过 | 尚未实施、未运行，当前CI通过只作旧实现基线 |
| 正式切换L | I通过＋具体试跑/切换窗口授权＋必要治理同步；旧轮安全处置，新轮命令冻结 | 未授权；不得先删树后补文档 |
| 正式轮次完成 | 来源耗尽事件与source_exhausted_met=true，以及字段/行为/正文图片/SQLite全部门禁 | 每个未来轮次独立验证，不以迁移CI/退出码/数量代替 |

## H｜未决项分层与设计状态

**P00准备状态以工作单P00-01–08为准。** v0.6补审修正了共享核心、活跃helper、动态import和依赖时点遗漏；
v0.7复核补齐执行器与共用fork的全量符号账（C8），修正锚点漂移并新增约束X1–X13。静态文档复核与新包安装/线上运行分开，
实现、切换及冻结治理授权均独立于设计准备状态。

| 分类/编号 | 证据与准确边界 | 解决阶段／不允许的推论 |
|---|---|---|
| 实施时验证 V1 | 静态报告不能证明迁入后资源/动态import/条件登录/取消等价 | T02/T05–T13按F矩阵验证，fork删除前全闭合；不提前补签 |
| 实施时验证 V2 | 原32文件417项、主线931/准备component934均旧基线 | T00保存node，T12重分组/静态导入，T13双版本；不重开#1/#2，不固定旧计数 |
| 实施时验证 V3 | 请求body/sign、原嵌套重试、TLS/状态读取需可控trace证实 | 每站F09；缺样本使用现有合成/净化fixture，不假称端点实测 |
| 另案风险 R01 | XT.finish清理异常后返回与provisional/pending磁盘摘要可能不同，#2未解决 | 终态边界专案：返回/落盘/租约一致性回归；不混入机械迁移或谎称已解决 |
| 另案风险 R02 | U:28/131现行容错、事件无fsync；E:143/BC:96另有强校验 | E3保持首期时点/分类；将来strict需要显式行为变更和故障矩阵，不强制本期全局传播 |
| 另案风险 R03 | DY search对象投影标aweme_detail；WB/ZH过滤后空；WB长文一次却异常attempts默认3 | 原实现/文档差异静态证据在C/E；先记录等价trace，若修来源判定/重试/耗尽条件须单独行为diff及授权，不用新抽象掩盖 |
| 另案风险 R04 | DY/WB投影可能过早丢头像证据；C.merge_repair_fallback_metadata:1186/1220有0被旧值覆盖分支；明确视频当前有root后置过滤 | T04/各站加入出口负例；若负例证实现行违规，单列修正、来源与行为差分审阅，不能以等价测试放过头像/视频边界，也不能称本轮已修 |
| 另案风险 R05 | registry:XHS warmed profile notes、知乎preferred_engine等是共享辅助定义，不是正式路径 | T10治理文案按实际调用方清理；不自动改driver/正式配置或重开XHS登录设计 |
| 另案风险 R06 | 上游380b426/cf513e7为抖音detail补`uifid/verifyFp/fp`参数及`x-tt-argus`头；fork公共参数已带前三者，无后者 | 首期机械迁移不引入；正式运行若出现“Blocked by ArgusSecurityPlugin”，按独立行为变更、F12补例并单独授权 |
| 后续可选研究 | 原30候选的许可/Python/外来切片尚未采用；本期不需新增包 | 保留主稿S23–S27历史，不作为当前设计阻塞；确需采用再独立补证 |
| 本轮文档差异 A05 | B站平台说明曾写详情时间优先、图片来源“优先”；实际hydrate保留搜索时间，图片按多源合并后fallback | 本轮仅纠正平台文案，C5保持源码行为；没有新增时间补缺或改变图片算法 |

设计状态：**v0.7静态复核完成，设计就绪D静态部分满足／未获实现授权**。五站首期完整交付不变，未来每站实现时同步迁测试；
内部顺序不授权停用其余平台。本文没有创建任何目标包、接口stub或改变当前运行行为。
