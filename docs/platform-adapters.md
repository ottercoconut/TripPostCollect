# 平台适配层本地化蓝图

> 本文是 MediaCrawler 依赖收编的设计图纸：把 `tools/MediaCrawler` 中仍在使用的平台实现迁入
> `src/trippostcollect/platforms/`，删除未用面，把上游降级为只读参照。完成谓词、停止状态和证据
> 门禁不变，仍由[正式抓取执行契约](formal-crawl-contract.md)定义；本文不重复契约语义。

## 背景与目标

`tools/MediaCrawler` 是 vendored 第三方仓库（上游 `NanmiCoder/MediaCrawler`，另在
`../MediaCrawler-upstream` 保留只读参照 clone）。本项目实际只消费其中约三成的平台层，并以
子进程 + 环境变量 + stdout 日志 + 被 hook 的 store 写出方法耦合。上游由 AI 高速产出、与本项目
契约冲突增多，继续作为独立仓库维护的协调成本已经超过其剩余情报价值。

目标：

- 五个平台（bilibili、weibo、douyin、zhihu、xhs）的 client/core/login/sign/model 迁入
  `src/trippostcollect/platforms/<platform>/`，删除 kuaishou、tieba、webui、store 后端、
  excel/api/recv_sms/proxy 等未用面；
- 全局可变 `config` 单例改为显式注入的配置对象；
- 子进程边界保留（浏览器进程隔离与 child 命令冻结仍是契约承重墙），入口从
  `runpy.run_path(tools/MediaCrawler/main.py)` 改为 `python -m trippostcollect.platforms.entry`；
- 正式抓取的可观察行为（字段、分页事件、manifest、JSONL 形状）逐字节等价，验证靠现有测试与
  dry-run，不靠信心；
- `tools/MediaCrawler` 迁移完成后整目录删除；上游跟踪走 `../MediaCrawler-upstream` 只读 diff。

## 当前耦合面（迁移的真实接缝）

`scripts/mediacrawler_export_entrypoint.py` 是 child 入口，现在做三件事：

1. `sys.path.insert(0, src/)`，让 MediaCrawler 模块可以反向 `import trippostcollect.*`；
2. 安装四个 monkey-patch：`install_export_hook`（导出边界递归清除头像键与重复 URL，失败关闭）、
   `install_xhs_repair_resilience`、`install_douyin_browser_detail_fallback`、
   `install_weibo_browser_detail_fallback`；
3. `runpy.run_path(tools/MediaCrawler/main.py)`，把本项目 CLI 参数原样透传给上游 main。

输入通道：`--platforms/--keywords/--login-type/...` CLI 参数 + 环境变量
（`TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH`、数据根、resume 摘要路径、cookie 导出等）。
输出通道：被 hook 的 store `write_to_jsonl/write_to_csv/write_single_item_to_json` 产物
（JSONL、分页事件、image_manifest.jsonl、staging 图片）+ stdout/stderr 日志。

本地化时 1 和 2 并入适配层自身模块；3 替换为本项目的显式入口函数，不再经过上游
`main.py`/`cmd_arg`/`config`。

## 目标拓扑

```text
src/trippostcollect/platforms/
├── registry.py                  # 现有：平台键、label、入口映射（继续扩展）
├── base.py                      # 吸收 base/base_crawler.py：AbstractCrawler + 共享会话协议
├── config.py                    # 新增：AdapterConfig 显式注入，替代全局可变 config
├── browser/
│   ├── launcher.py              # ← tools/browser_launcher.py
│   ├── cdp.py                   # ← tools/cdp_browser.py
│   └── stealth.py               # ← libs/stealth.min.js 装载器
├── evidence/
│   ├── adaptive.py              # ← tools/trippostcollect_adaptive.py（自适应分页/来源耗尽）
│   ├── behavior.py              # ← tools/trippostcollect_behavior.py（行为证据）
│   ├── image_manifest.py        # ← tools/image_manifest.py
│   ├── image_download_retry.py  # ← tools/image_download_retry.py
│   └── user_hash.py             # ← tools/user_hash.py
├── export/
│   └── entry.py                 # ← scripts/mediacrawler_export_entrypoint.py 的钩子与入口
│                                #   （头像消毒、三平台兜底、store JSONL 写出）
├── bilibili/  weibo/  douyin/  zhihu/  xhs/
│   ├── signer.py                # 各平台签名收口（原 client.py 内嵌签名逻辑抽出）
│   ├── client.py                # 签名 HTTP 客户端（请求/详情/作者 API）
│   ├── core.py                  # 搜索分页 + 详情/作者补全循环
│   ├── login.py                 # cookie/二维码/人工登录流
│   ├── fields.py                # ← field.py + help.py（字段归一、HTML 提取）
│   ├── models.py                # ← model/m_<platform>.py
│   ├── errors.py                # ← exception.py（运行级 vs 候选级错误类型）
│   └── assets.py                # ← libs/<platform>.js 等静态资产路径
└── entry.py                     # `python -m trippostcollect.platforms.entry`：显式参数 +
                                 # 环境变量 → 调 registry → 平台 core → export 产物
```

`entry.py` 保留子进程形态：runner 侧命令从
`python scripts/mediacrawler_export_entrypoint.py ...` 变为
`python -m trippostcollect.platforms.entry ...`。参数语义、环境变量名、输出文件布局不变；
已验收的历史 execution state 不要求重跑。

## 逐文件迁移映射

### 平台层（`media_platform/<p>/` → `platforms/<p>/`）

| 源 | 目标 | 动作 |
|---|---|---|
| `client.py` | `client.py` + `signer.py` | 签名逻辑抽出到 `signer.py`；`config.` 读改注入；保留错误类型 |
| `core.py` | `core.py` | 原样搬入后逐段改 import；不动分页/详情语义 |
| `login.py` | `login.py` | 同上 |
| `help.py`/`field.py`/`extractor.py`(xhs) | `fields.py` | 合并归一；知乎 `extract_zhihu_content_text` 随之迁移 |
| `exception.py` | `errors.py` | 改名对齐项目语义，保留类型 |
| `model/m_<p>.py` | `models.py` | 搬入；xhs/weibo/zhihu/douyin/bilibili 各一 |
| `constant/zhihu.py` | `zhihu/` 内常量 | 搬入；`baidu_tieba` 删除 |
| `libs/douyin.js`/`zhihu.js`/`stealth.min.js` | `assets/`（包数据文件） | 随包发布；pyexecjs 调用点见决策 D2 |
| xhs `playwright_sign.py`/`xhs_sign.py`/`manual_wait.py`/`search_safety.py`(dy) | 对应 `signer.py`/`login.py`/`core.py` 内 | xhs 签名继续薄壳 `xhshow`；dy 安全页检测随 core |

### 共享层

| 源 | 目标 | 动作 |
|---|---|---|
| `base/base_crawler.py` | `platforms/base.py` | 保留 ABC；头部 license 注释随文件保留 |
| `tools/browser_launcher.py`、`tools/cdp_browser.py` | `platforms/browser/` | 本项目已深度改造，原样搬 |
| `tools/trippostcollect_*.py`、`image_*`、`user_hash.py` | `platforms/evidence/` | 原生项目代码，搬 |
| `tools/crawler_util.py`/`httpx_util.py`/`slider_util.py`/`easing.py`/`time_util.py`/`utils.py`/`words.py`/`async_file_writer.py`/`file_header_manager.py` | 按实际被引用处就近归位 | 逐引用核对；无引用即删（`slider_util` 的 cv2 依赖待确认存活） |
| `store/<p>/` JSONL 写出器 | `platforms/export/` | 保留 JSONL 写出；CSV/DB/excel 后端全删 |
| `main.py`/`cmd_arg/`/`config/` | **删除** | 由 `entry.py` + `AdapterConfig` 替代 |
| `api/`、`webui/`、`media_platform/{kuaishou,tieba}`、`store` 其他后端、`recv_sms.py`、`proxy/`、`database/`、`cache/`、`browser_data/` | **删除** | 未用面整体丢弃 |
| `app_runner.py` | 删除或并入 `entry.py` | 视其被引用程度定 |

### `config.` 去耦

现状 26 个文件 327 处 `config.XXX` 读取，全部由 CLI 参数在 `main.py` 启动时改写全局模块属性。
迁移方案：`AdapterConfig`（pydantic 或 dataclass）在 `entry.py` 由显式参数 + 环境变量构造，
沿 `core(client(config))` 一层层传入。步骤：

1. 先机械扫描每个 `config.X` 字段的真实使用平台集合（327 处中大量是跨平台公共项）；
2. `AdapterConfig` 字段全集 = 实际被引用项；只被死重引用的字段随文件一起删；
3. client/core 签名改为 `XxxClient(config: AdapterConfig, ...)`，不再读模块全局；
4. 单测保证每个平台 config 字段无遗漏（启动时断言必需字段非 None，替代运行期 KeyError）。

### 入口与 hook 归位

`mediacrawler_export_entrypoint.py` 的四个 `install_*` 是本项目的资产，并入 `platforms/export/`：

- 导出头像消毒（失败关闭）保留为 export 层强制步骤，不再以 monkey-patch 形态存在——直接改
  JSONL 写出器本体；
- xhs/douyin/weibo 三个浏览器详情兜底改成对应 `client.py` 的显式 fallback 方法；
- `sys.path` 魔术消失：包内 import 自然解析。

## 测试迁移

`tools/MediaCrawler/tests/` 44 个文件按存活代码归属搬进 `tests/platforms/`：

- xhs 15 个文件几乎全存活（登录状态机、checkpoint、行为）；
- 其余 29 个按平台分拣；只覆盖死重的测试（store 后端、webui、kuaishou/tieba 路径）随死重删；
- 依赖外部环境（Redis/MongoDB/真浏览器）的测试标注现有跳过规则；
- 根仓 `tests/` 现有 40 个文件不动；迁移后每个平台要求：`pytest tests/platforms/<p>` 与
  根仓 `python -m pytest` 同时通过。

## 依赖合并

根 `pyproject.toml` 增：`httpx`、`parsel`、`xhshow>=0.2.0`、`tenacity`（视实际引用）。
`pyexecjs` 与 Node.js 依决策 D2 去留；`opencv-python` 视 `slider_util` 存活判定。
不引入：`aiomysql/redis/motor/asyncmy/sqlalchemy/alembic/fastapi/uvicorn/pandas/openpyxl/
matplotlib/jieba/wordcloud/typer/aiofiles/pyhumps/python-dotenv/aiosqlite/requests`。

浏览器基座：正式结构化链路继续用 `playwright` 还是统一 `patchright`，作为独立决策项
（换驱动改变指纹证据基线，不与本地化捆绑）。

## 命名化石

以下名称保留原样并在首次出现处注明"名称来源于已移除的第三方实现"：

- `job_kind=mediacrawler_search`（`crawl_jobs` CHECK 约束与存量行）；
- `scripts/mediacrawler_crawl.py`、`mediacrawler_export_entrypoint.py`、`mediacrawler_behavior.py`、
  `mediacrawler_login_warmup.py` 脚本名（runner 命令与文档引用面太大，改名不划算）；
- `outputs/mediacrawler_runs/`（历史产物路径契约）；
- `mediacrawler_login_output`/`MEDIACRAWLER_*` 等路径常量。

改名收益为零、追溯成本为正，不做。

## 上游哨兵规程

`../MediaCrawler-upstream` 为只读 clone。平台字段/签名疑似失效或定期（每月）检查：

```bash
git -C ../MediaCrawler-upstream fetch origin main
git -C ../MediaCrawler-upstream log --oneline <上次审计SHA>..origin/main
git -C ../MediaCrawler-upstream diff <上次审计SHA>..origin/main -- media_platform/<平台>/
```

只看在用平台的 client/login/sign 相关 diff；`feat(media)` 级重构整批跳过；测试断言不移植，
移植后按本项目契约改写。每次审计把"上次审计 SHA"更新进本文档末尾的审计锚点。

## 分阶段计划

| 阶段 | 内容 | 验证门禁 |
|---|---|---|
| 0 | 可选：先把 `tools/MediaCrawler` 以 subtree 并入根仓保留历史（双仓税即消，后续仍按本蓝图重构）；也可跳过直接进阶段 1 | import 路径与测试路径修正后 pytest 全绿 |
| 1 | 共享层 + `AdapterConfig` + bilibili 试点（最小平台，趟模板） | `pytest tests/platforms/bilibili` + 根仓 pytest + bilibili `--no-import` 低频诊断 |
| 2 | weibo | 同上门禁（weibo 诊断） |
| 3 | zhihu（含 D2 签名决策落地） | 同上（zhihu 诊断 + `--zhihu-detail-urls-file` 路径） |
| 4 | douyin（XHR 监听 + 游标链最复杂，单独验证） | 同上（douyin 诊断） |
| 5 | xhs（最重：`core.py` 3k 行、租约/扫码/单 Context 体系） | 根仓 pytest + `xhs_runner.py --dry-run` 四阶段冻结核对 |
| 6 | `tools/MediaCrawler` 整目录删除、依赖裁剪、文档冻结窗口 | 全量验证 + `verify_frozen_files.py` |

每阶段一个根仓提交；`outputs/`、`data/`、SQLite 不动。任一阶段失败回滚到上一阶段提交点。

## 决策记录

| # | 决策 | 状态 | 结论 |
|---|---|---|---|
| D1 | 冻结文档（crawl-architecture/data-persistence/AGENTS.md）更新授权 | 待批 | 迁移收尾时同一窗口处理，diff 过目后授权 |
| D2 | 知乎签名：`pyexecjs`+打包 JS 保留 vs 浏览器内执行（去 Node 依赖、与 douyin 同构） | 待定 | 阶段 3 开工前定；默认先保留 execjs 保等价，浏览器执行列为后续独立演进 |
| D3 | 子进程边界 | 已定 | 保留；入口改 `python -m trippostcollect.platforms.entry` |
| D4 | 上游跟踪姿态 | 已定 | `../MediaCrawler-upstream` 只读 diff 哨兵，不合并 |
| D5 | 主链路 playwright → patchright 统一 | 待定 | 与本地化解耦；指纹证据基线变化需单独评估 |
| D6 | `slider_util`/opencv 存活 | 待核 | 阶段 1 扫描引用后定 |

## 审计锚点

- 上游 `origin/main` 已审计至：`380b426`（2026-09-26 clone）。
- 迁移前 MediaCrawler 基线：`main @ 2bcde689cc9a1b50cbcc7255597e9da1ff1a1b30`。
