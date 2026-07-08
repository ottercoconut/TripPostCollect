# TripPostCollect 管理端阶段提示词

本文基于 `docs/admin-client-development.md`，用于后续在 goal 模式中分阶段推进。每次只复制一个阶段提示词，完成、验证、提交后再进入下一阶段。

## 通用边界

所有阶段都必须遵守：

- 先读 `docs/README.md` 和 `docs/admin-client-development.md`。
- 首版管理端 HTTP API 只读，不提供记录、图片、证据、平台、调度表写入能力。
- 前端不触发 bootstrap、sync、dry-run 或真实抓取。
- 图片和证据文件只按数据库 ID 或枚举 `kind` 读取，不接受任意 URL/path。
- raw JSON 分层：`web_posts` 的 `raw_sample_json/metrics_json/author_json`，`ctf_captures` 的 `raw_meta_json` 等证据 JSON。
- 新 Python 代码通过 `src/trippostcollect/` 包复用，不长期直接 import `scripts/*.py`。
- Python 命令先 `source .venv/bin/activate`，再使用 `python`。
- 临时库和临时产物放 `temp/<task>/`，验证后清理。

## 阶段 0：实施前复核

```text
进入 goal 模式后，先只做管理端实施前复核，不写代码。

目标：
- 阅读 docs/README.md 和 docs/admin-client-development.md。
- 复核首版只读边界、图片/文件代理安全边界、raw JSON 字段归属、Python 包安装方案。
- 输出实际实施顺序、每阶段风险、预计修改文件和验证方式。

验收：
- 不修改文件。
- 明确下一阶段从 pyproject.toml + src/trippostcollect 包骨架开始。
```

## 阶段 1：共享 Python 包和兼容入口

```text
按 docs/admin-client-development.md 实现共享 Python 包骨架和旧脚本兼容入口。

范围：
- 新增 pyproject.toml，使用 src layout，包名 trippostcollect。
- 新增 src/trippostcollect/ 包骨架：core、db、platforms、records、artifacts、scheduler。
- 抽取 core.paths、db.bootstrap、platforms.registry 的最小可用版本。
- scripts/project_paths.py、scripts/db_bootstrap.py、scripts/web_sites.py 保持兼容，不破坏现有脚本入口。
- 不迁移抓取逻辑，不改默认数据库，不恢复废弃平台。

验证：
- source .venv/bin/activate
- python -m pip install -e .
- python -m py_compile 相关新增/改动 Python 文件。
- 临时 SQLite bootstrap 后确认 source_platforms=8、crawl_jobs=8。
- python scripts/crawl_runner.py --dry-run --max-jobs 5。
- 清理本阶段 temp 产物。
```

## 阶段 2：FastAPI 只读后端 API

```text
实现 apps/admin_api 的只读 FastAPI 后端和核心记录 API。

范围：
- 新增 apps/admin_api/app/main.py、settings.py、deps.py、routers/。
- DB 连接使用只读 mode=ro，短连接，row_factory=sqlite3.Row，busy_timeout。
- 启动时只做 schema/status 检查，不执行 bootstrap_database，不同步 source_platforms/crawl_jobs。
- 实现 GET /api/health、/api/meta、/api/maintenance/schema-status。
- 实现 GET /api/records、/api/records/{id}、/api/records/{id}/context、/api/records/{id}/raw。
- 记录列表支持平台、城市、关键词、状态、时间、缺字段、分页、排序。
- raw JSON 按 record/capture 分层，JSON 解析失败只返回错误信息，不修写数据库。
- 不提供任何 POST/PATCH/DELETE。

验证：
- source .venv/bin/activate
- python -m py_compile apps/admin_api/app/**/*.py src/trippostcollect/**/*.py。
- 使用临时库或测试客户端验证 records/meta/schema-status。
- 路由表确认没有 POST/PATCH/DELETE。
- 模拟抓取脚本写入新记录后，下一次 API 查询能读到最新数据。
```

## 阶段 3：图片、证据和调度只读 API

```text
补齐管理端图片预览、证据读取、调度和概览只读 API。

范围：
- 实现 GET /api/records/{id}/images、/api/images/{id}/preview。
- 实现 GET /api/captures、/api/captures/{id}、/api/captures/{id}/images、/api/captures/images/{id}/preview。
- 实现 GET /api/captures/{id}/artifact?kind=screenshot|visible_text|rendered_html。
- 本地路径必须 resolve 后仍在允许目录内，拒绝 ..、项目外路径、符号链接逃逸。
- 远程图片只允许数据库已有 http/https URL；如后端代取，禁止 localhost、内网 IP、link-local，限制重定向、超时、大小和 Content-Type。
- 实现 GET /api/overview/*、/api/platforms、/api/scheduler/config、/api/scheduler/jobs、/api/scheduler/reports。
- 只读展示当前 8 个平台和 8 个启用任务。
- 不触发 bootstrap、sync、dry-run 或真实抓取。

验证：
- source .venv/bin/activate
- python -m py_compile 相关 Python 文件。
- 临时库构造本地图片、远程图片、项目外路径、符号链接逃逸样本。
- 合法图片/证据可读，非法路径和任意 URL 被拒绝。
- 概览、平台、调度和运行报告 API 返回预期数据。
```

## 阶段 4：后端测试和只读边界审计

```text
为管理端后端补测试，锁定只读边界和实时读取行为。

范围：
- 使用 temp/admin_client_verify.sqlite 构造临时库。
- 覆盖 records list/detail/context/raw、images preview、captures artifact、overview、scheduler。
- 测试不存在 POST/PATCH/DELETE 写入记录、图片、证据、平台或调度表的接口。
- 测试管理端不执行 bootstrap、sync、dry-run 或真实抓取。
- 测试新记录写入后，下一次 API 查询能读到最新数据。

验证：
- source .venv/bin/activate
- python -m py_compile 相关 Python 文件。
- 运行本阶段新增的测试命令。
- 删除 temp/admin_client_verify.sqlite 和本阶段 temp 产物。
```

## 阶段 5：React/Vite 前端首版

```text
实现 apps/admin_web 的 React/Vite 首版只读管理端。

范围：
- 初始化 React + TypeScript + Vite，配置 /api proxy 到 http://127.0.0.1:8787。
- 参考 docs/admin-client-record-workbench-template.html，实现 AppShell、左侧导航、顶部 DB/只读状态栏。
- 默认首页是记录工作台，不做 landing page。
- 实现平台、城市、关键词、状态、时间、缺字段筛选和记录表格。
- 实现选中记录详情：图片预览、作者、互动、证据摘要、JSON 懒加载。
- 实现手动刷新和合理轮询，运行报告只读展示。
- UI 中不得出现保存、新增、删除、sync、dry-run、真实抓取按钮。

验证：
- npm install
- npm run build
- 启动 dev server，并用浏览器截图检查记录工作台、详情、图片预览、运行报告。
- 检查 1280px 和窄屏布局不重叠。
```

## 阶段 6：质量视图和首版打磨

```text
完成只读管理端首版打磨和文档同步。

范围：
- 实现数据质量定位视图：缺图片、缺发布时间、缺作者粉丝量等问题记录。
- 问题记录可跳转到记录详情。
- 打磨图片加载中、加载失败、本地缺失、远程 URL 状态。
- 页面说明需要修改数据时，应使用后续受控终端脚本，不在前端写库。
- 更新 docs/admin-client-development.md 中已实现/待实现状态。

验证：
- 后端测试通过。
- npm run build 通过。
- 浏览器桌面和窄屏截图检查布局不重叠。
- 全局搜索确认 UI 和 API 中没有保存、新增、删除、sync、dry-run、真实抓取入口。
```
